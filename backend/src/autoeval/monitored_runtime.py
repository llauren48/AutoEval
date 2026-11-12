"""Linux container job runtime. Invoked only with a pinned deployment root and UUID."""

import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import uuid
from pathlib import Path


def atomic(path, body):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(body))
    os.replace(tmp, path)


def identity(pid):
    return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]


def alive(row):
    try:
        fields = Path(f"/proc/{row['pid']}/stat").read_text().rsplit(")", 1)[1].split()
        return fields[0] != "Z" and fields[19] == row["identity"]
    except OSError:
        return False


def validate_budget(request):
    budget = request.get("budget") or {}
    maximum = budget.get("max_model_requests")
    output_limit = budget.get("max_output_tokens")
    params = request["config"]["params"]
    if (
        type(maximum) is not int
        or not 1 <= maximum <= 5
        or type(output_limit) is not int
        or not 1 <= output_limit <= 2048
    ):
        raise ValueError("Invalid frozen model budget")
    if (
        type(params.get("num_prompts")) is not int
        or not 1 <= params["num_prompts"] <= maximum
        or params.get("retry") != 1
        or params.get("batch_size") != 1
        or params.get("max_num_workers") != 1
        or type(params.get("max_out_len")) is not int
        or not 1 <= params["max_out_len"] <= output_limit
    ):
        raise ValueError("Tool configuration exceeds the frozen model budget")


def scrub(job):
    keys = set()
    for name in ["request.json", "input.json"]:
        path = job / name
        if path.exists():
            data = json.loads(path.read_text())
            key = data.get("api_key") or data.get("params", {}).get("api_key")
            if key and key != "[REDACTED]":
                keys.add(key)
    for item in job.rglob("*"):
        if item.is_symlink():
            raise ValueError("Symbolic artifact rejected")
        if item.is_file():
            if item.stat().st_size > 12 * 1024 * 1024:
                raise ValueError("Artifact exceeds sanitization bound")
            try:
                original = item.read_text()
            except UnicodeError:
                continue
            cleaned = original
            for key in keys:
                cleaned = cleaned.replace(key, "[REDACTED]")
            if cleaned != original:
                item.write_text(cleaned)


def status(job):
    cancellation = job / "cancel-targets.json"
    if cancellation.exists():
        records = json.loads(cancellation.read_text())
        if all(not alive(row) for row in records):
            scrub(job)
            atomic(job / "exit.json", {"state": "cancelled", "exit_code": None})
            return {"state": "cancelled"}
        return {"state": "running", "cancellation_requested": True}
    if (job / "exit.json").exists():
        return json.loads((job / "exit.json").read_text())
    if not (job / "process.json").exists():
        return {"state": "unknown"}
    row = json.loads((job / "process.json").read_text())
    try:
        if (
            row["boot"] != Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            or identity(row["pid"]) != row["identity"]
        ):
            return {"state": "unknown"}
    except OSError:
        return {"state": "unknown"}
    return {"state": "running", "process_id": row["pid"]}


def child(root, job):
    request = json.loads((job / "request.json").read_text())
    validate_budget(request)
    key = request.pop("api_key")
    if not isinstance(key, str) or not key:
        raise ValueError("Credential missing")
    config = request["config"]
    config["params"]["api_key"] = key
    path = job / "input.json"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(config, stream)
    (job / "request.json").unlink()
    env = {
        **os.environ,
        "PYTHONPATH": f"{root}/src:{root}/benchmark",
        "PYTHONUNBUFFERED": "1",
        "AE_MONITOR_FINAL": str(job / "monitor-final.json"),
        "AE_MONITOR_READY": str(job / "monitor-ready"),
    }
    with (job / "job.log").open("w") as log:
        proc = subprocess.Popen(
            [sys.executable, "-m", "autoeval_adaptor", "--config", str(path)],
            env=env,
            cwd=root / "benchmark",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        for line in proc.stdout:
            log.write(line.replace(key, "[REDACTED]"))
            log.flush()
        code = proc.wait()
    # Sanitise generated tool configuration before any archival or collection.
    for item in job.rglob("*"):
        if item.is_file() and not item.is_symlink() and item.stat().st_size < 16 * 1024 * 1024:
            try:
                text = item.read_text()
                if key in text:
                    item.write_text(text.replace(key, "[REDACTED]"))
            except (UnicodeError, OSError):
                pass
    atomic(job / "exit.json", {"state": "succeeded" if code == 0 else "failed", "exit_code": code})


def main():
    root = Path(sys.argv[2]).resolve()
    job = root / "jobs" / str(uuid.UUID(sys.argv[3]))
    action = sys.argv[1]
    if action == "child":
        child(root, job)
        return
    if action == "submit":
        request = json.load(sys.stdin)
        validate_budget(request)
        for relative, sha in request["pins"].items():
            if hashlib.sha256((root / relative).read_bytes()).hexdigest() != sha:
                raise ValueError("Pinned tool files changed")
        if job.exists():
            print(json.dumps(status(job)))
            return
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", request["config"]["params"]["monitor_port"]))
        job.parent.mkdir(exist_ok=True)
        if job.exists():
            print(json.dumps(status(job)))
            return
        job.mkdir(mode=0o700)
        descriptor = os.open(job / "request.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            json.dump(request, stream)
        proc = subprocess.Popen(
            [sys.executable, __file__, "child", str(root), job.name],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        atomic(
            job / "process.json",
            {
                "pid": proc.pid,
                "identity": identity(proc.pid),
                "boot": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            },
        )
        print(json.dumps(status(job)))
    elif action == "status":
        print(json.dumps(status(job)))
    elif action == "collect":
        if status(job)["state"] not in {"succeeded", "failed"}:
            raise ValueError("Job outcome not confirmed")
        result = json.loads((job / "output/adaptor_result.json").read_text())
        output = Path(result["ais_bench_work_dir"]).resolve()
        if not output.is_relative_to(job / "output"):
            raise ValueError("Output ownership mismatch")
        env = {**os.environ, "PYTHONPATH": f"{root}/src:{root}/benchmark"}
        done = subprocess.run(
            [
                sys.executable,
                "-m",
                "autoeval_adaptor",
                "--config",
                str(job / "input.json"),
                "--collect-results",
                "--result-dir",
                str(output),
                "--output",
                str(job / "unified_result.json"),
            ],
            check=False,
            env=env,
            cwd=root / "benchmark",
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if done.returncode:
            raise ValueError("Result collection failed")
        print(json.dumps({"state": "collected", "output": str(output)}))
    elif action == "bundle":
        if status(job)["state"] not in {"succeeded", "failed", "cancelled"}:
            raise ValueError("Cannot archive an active job")
        files = []
        size = 0
        for item in sorted(job.rglob("*")):
            if item.is_symlink():
                raise ValueError("Symbolic artifact rejected")
            if item.is_file() and item.name != "raw-artifacts.json":
                raw = item.read_bytes()
                size += len(raw)
                if size > 12 * 1024 * 1024:
                    raise ValueError("Artifacts exceed collection bound")
                files.append(
                    {
                        "path": str(item.relative_to(job)),
                        "sha256": hashlib.sha256(raw).hexdigest(),
                        "size": len(raw),
                        "content": raw.decode("utf-8"),
                    }
                )
        atomic(job / "raw-artifacts.json", {"run_id": job.name, "files": files})
        print(json.dumps({"state": "bundled", "files": len(files)}))
    elif action == "cleanup":
        if not job.is_relative_to(root / "jobs") or job.is_symlink():
            raise ValueError("Cleanup ownership mismatch")
        if job.exists():
            if status(job)["state"] not in {"succeeded", "failed", "cancelled"}:
                raise ValueError("Job is still occupied")
            shutil.rmtree(job)
        print(json.dumps({"state": "cleaned"}))
    elif action == "cancel":
        if status(job)["state"] != "running":
            print(json.dumps(status(job)))
            return
        owner = json.loads((job / "process.json").read_text())
        if not alive(owner):
            raise ValueError("Process owner no longer matches")
        # Only descendants of the verified owner are eligible. Record birth identities
        # before signalling; PID reuse can never authorise killing another process.
        records = {owner["pid"]: {"pid": owner["pid"], "identity": owner["identity"]}}
        for _ in range(16):
            added = False
            for entry in Path("/proc").iterdir():
                if not entry.name.isdigit():
                    continue
                try:
                    fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
                    pid = int(entry.name)
                    if int(fields[1]) in records and pid not in records:
                        records[pid] = {"pid": pid, "identity": fields[19]}
                        added = True
                except OSError:
                    continue
            if not added:
                break
        atomic(job / "cancel-targets.json", list(records.values()))
        for row in reversed(list(records.values())):
            if alive(row):
                try:
                    os.kill(row["pid"], signal.SIGTERM)
                except ProcessLookupError:
                    continue
        print(json.dumps(status(job)))
    else:
        raise ValueError("Unknown operation")


if __name__ == "__main__":
    main()
