"""Copied into the Linux executor. Fixed framework smoke only, never model scoring."""

import fcntl
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(os.environ.get("AE_JOB_ROOT", "/work/autoeval-jobs"))


def atomic_json(path, value):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value), encoding="utf-8")
    os.replace(tmp, path)


def write_credential(path, credential):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump({"value": credential}, stream)


def identity(pid):
    # /proc stat's comm may contain spaces or parentheses.
    return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]


def inspect_job(folder):
    if (folder / "cleanup.json").exists():
        cleanup = json.loads((folder / "cleanup.json").read_text())
        if cleanup["state"] == "cleaned":
            return {"state": "cleaned"}
        return {"state": "unknown", "cleanup_pending": True}
    if (folder / "exit.json").exists():
        return json.loads((folder / "exit.json").read_text())
    if not (folder / "job.json").exists():
        return {"state": "unknown"}
    job = json.loads((folder / "job.json").read_text())
    try:
        if job["boot_id"] != Path("/proc/sys/kernel/random/boot_id").read_text().strip():
            return {"state": "unknown"}
        if identity(job["pid"]) != job["start_time"]:
            return {"state": "unknown"}
    except OSError:
        return {"state": "unknown"}
    return {"state": "running", "pid": job["pid"]}


def child(folder):
    os.umask(0o077)
    spec = json.loads((folder / "spec.json").read_text())

    def cancelled(signum, frame):
        atomic_json(folder / "exit.json", {"state": "cancelled"})
        raise SystemExit(0)

    if spec.get("kind") != "framework_smoke":
        from planning_runtime import controlled_failure_reason, execute

        def cancel_controlled(signum, frame):
            (folder / "credential.json").unlink(missing_ok=True)
            (folder / "private-config.json").unlink(missing_ok=True)
            harbor = (
                spec.get("compilation", {}).get("config", {}).get("subcommand") == "harbor_agent"
            )
            atomic_json(folder / "exit.json", {"state": "cancelled", "cleanup_pending": harbor})
            raise SystemExit(0)

        signal.signal(signal.SIGTERM, cancel_controlled)
        try:
            result = execute(folder, spec)
            atomic_json(folder / "result.json", result)
            atomic_json(folder / "exit.json", {"state": "succeeded"})
        except Exception as exc:  # noqa: BLE001 -- isolate third-party errors without leaking credentials
            # Persist only allowlisted reason codes, never third-party response text.
            atomic_json(
                folder / "exit.json",
                {"state": "failed", "reason": controlled_failure_reason(spec, exc)},
            )
        return
    signal.signal(signal.SIGTERM, cancelled)
    time.sleep(spec["duration_seconds"])
    atomic_json(
        folder / "result.json",
        {"kind": "framework_smoke", "model_called": False, "measurement": None},
    )
    atomic_json(folder / "exit.json", {"state": "succeeded"})


def main():
    action, key = sys.argv[1:3]
    uuid.UUID(key)
    if str(uuid.UUID(key)) != key:
        raise ValueError("Canonical UUID required")
    ROOT.mkdir(parents=True, exist_ok=True)
    folder = ROOT / key
    if action == "child":
        child(folder)
        return
    if action != "submit" and not folder.exists():
        state = {"state": "unknown"}
        if action == "cleanup":
            state["cleaned"] = False
        print(json.dumps(state))
        return
    folder.mkdir(exist_ok=True)
    if folder.is_symlink() or folder.resolve().parent != ROOT.resolve():
        raise ValueError("Invalid job directory")
    with (folder / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if action == "submit":
            if (folder / "cleanup.json").exists():
                raise ValueError("Cleaned submission key cannot be reused")
            spec = json.load(sys.stdin)
            credential = None
            if spec.get("kind") == "framework_smoke":
                if (
                    set(spec) != {"kind", "duration_seconds"}
                    or type(spec["duration_seconds"]) is not int
                    or not 0 <= spec["duration_seconds"] <= 30
                ):
                    raise ValueError("Invalid smoke specification")
            else:
                from planning_runtime import validate_spec

                validate_spec(spec)
                credential = spec.pop("credential")
                spec["credential"] = "runtime-only"
            digest = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()
            if (folder / "spec_hash").exists():
                if (folder / "spec_hash").read_text() != digest:
                    raise ValueError("Submission key reused with different specification")
            else:
                (folder / "spec_hash").write_text(digest)
                folder.chmod(0o700)
                if credential is not None:
                    secret_path = folder / "credential.json"
                    write_credential(secret_path, credential)
                atomic_json(folder / "spec.json", spec)
                with (folder / "output.log").open("ab") as log:
                    proc = subprocess.Popen(
                        [sys.executable, __file__, "child", key],
                        stdin=subprocess.DEVNULL,
                        stdout=log,
                        stderr=log,
                        start_new_session=True,
                    )
                atomic_json(
                    folder / "job.json",
                    {
                        "pid": proc.pid,
                        "start_time": identity(proc.pid),
                        "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                    },
                )
        elif action == "cancel":
            state = inspect_job(folder)
            if state["state"] == "running":
                os.killpg(state["pid"], signal.SIGTERM)
        elif action == "cleanup":
            cleanup_path = folder / "cleanup.json"
            cleanup = json.loads(cleanup_path.read_text()) if cleanup_path.exists() else None
            if cleanup is not None and cleanup["state"] == "cleaned":
                print(json.dumps({"state": "cleaned", "cleaned": True}))
                return
            if cleanup is None:
                state = inspect_job(folder)
                spec_path = folder / "spec.json"
                saved_spec = json.loads(spec_path.read_text()) if spec_path.exists() else {}
                harbor = (
                    saved_spec.get("compilation", {}).get("config", {}).get("subcommand")
                    == "harbor_agent"
                )
                if harbor and state["state"] in {"cancelled", "failed"}:
                    print(
                        json.dumps(
                            {
                                **state,
                                "cleaned": False,
                                "cleanup_pending": True,
                                "reason": "Harbor external container ownership must be verified before reclaim",
                            }
                        )
                    )
                    return
                if state["state"] not in {"succeeded", "failed", "cancelled"}:
                    print(json.dumps({**state, "cleaned": False}))
                    return
                observed_state = state["state"]
                atomic_json(cleanup_path, {"state": "cleaning", "observed_state": observed_state})
            else:
                observed_state = cleanup["observed_state"]
            for path in folder.iterdir():
                if path.name not in {".lock", "cleanup.json"}:
                    if path.is_dir():
                        # Artifacts remain in the owned job directory; no recursive deletion.
                        continue
                    path.unlink()
            atomic_json(cleanup_path, {"state": "cleaned"})
            print(
                json.dumps(
                    {
                        "state": "cleaned",
                        "observed_state": observed_state,
                        "cleaned": True,
                    }
                )
            )
            return
        elif action == "collect":
            state = inspect_job(folder)
            if state["state"] == "succeeded":
                state["result"] = json.loads((folder / "result.json").read_text())
            print(json.dumps(state))
            return
        elif action != "status":
            raise ValueError("Unknown action")
        print(json.dumps(inspect_job(folder)))


if __name__ == "__main__":
    main()
