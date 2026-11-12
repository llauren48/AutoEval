"""Actual HTTP/SSE against a local artificial server; no paid endpoints."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import autoeval_planner.probe as planner_probe
import pytest
from autoeval_planner.errors import ProbeError

from autoeval.planning_runtime import (
    ADAPTOR_REVISION,
    PLANNER_REVISION,
    controlled_failure_reason,
    execute,
    inspect_dataset,
    installed_tool_versions,
)


def test_preflight_reports_tool_versions_paths_and_dataset_blockers(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "autoeval.planning_runtime.installed_revision",
        lambda name: PLANNER_REVISION if name == "autoeval-task-planner" else ADAPTOR_REVISION,
    )
    versions = installed_tool_versions()
    assert versions["planner"]["actual"] == PLANNER_REVISION
    assert versions["adaptor"]["actual"] == ADAPTOR_REVISION

    task = {
        "task_id": "gpqa",
        "dataset": "GPQA",
        "compilation": {
            "config": {
                "subcommand": "llm_accuracy",
                "params": {"ais_bench_root": "/tool"},
            },
            "runtime_config": {"datasets_dir": str(tmp_path)},
        },
        "tool": {"revision": "a" * 40},
    }
    monkeypatch.setattr("autoeval.planning_runtime.verify_tool", lambda value: "a" * 40)
    checked = inspect_dataset(task, networks=None, validated={"GPQA"})
    assert checked["data_path"] == str(tmp_path)
    assert checked["tool_revision"] == "a" * 40
    assert checked["blocking_reasons"] == []

    blocked = inspect_dataset(
        {
            **task,
            "task_id": "deepswe",
            "dataset": "DeepSWE",
            "compilation": {
                "config": {"subcommand": "harbor_agent", "params": {"ais_bench_root": "/tool"}}
            },
        },
        networks=None,
        validated=set(),
    )
    assert blocked["blocking_reasons"] == [
        "dataset_not_validated",
        "dataset_path_missing",
        "harbor_network_unavailable",
    ]


@pytest.mark.parametrize(
    "mode",
    ["sse", "fallback", "missing_usage", "rate_limit", "auth", "timeout", "incompatible"],
)
def test_probe_sse_fallback_missing_usage_and_failure(tmp_path, monkeypatch, mode):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            assert self.headers["Authorization"] == "Bearer TEST-only"
            if mode == "timeout":
                time.sleep(0.2)
                try:
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"{}")
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            if (
                mode in {"rate_limit", "auth", "incompatible"}
                or mode == "fallback"
                and body.get("stream")
            ):
                status = {"rate_limit": 429, "auth": 401}.get(mode, 400)
                self.send_response(status)
                self.end_headers()
                self.wfile.write(b"TEST-failure")
                return
            if body.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                chunks = [{"choices": [{"delta": {"content": "TEST artificial response"}}]}]
                if mode != "missing_usage":
                    chunks.append({"choices": [], "usage": {"completion_tokens": 8}})
                for chunk in chunks:
                    self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
                self.wfile.write(b"data: [DONE]\n\n")
            else:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(
                    json.dumps(
                        {
                            "choices": [{"message": {"content": "TEST artificial"}}],
                            "usage": {"completion_tokens": 8},
                        }
                    ).encode()
                )

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    spec = {
        "kind": "planner_probe",
        "credential": "runtime-only",
        "options": {
            "base_url": f"http://127.0.0.1:{server.server_port}/v1",
            "model": "TEST-local-only",
            "concurrency_levels": [1],
            "requests_per_level": 1,
            "prompt_tokens": 8,
            "max_tokens": 16,
        },
    }
    (tmp_path / "credential.json").write_text('{"value":"TEST-only"}')
    monkeypatch.setattr(
        "autoeval.planning_runtime.installed_revision",
        lambda name: "0ee9abf49a57c5accc62df8900693cdcea88ea9a",
    )
    if mode == "timeout":
        original = planner_probe.run_probe
        monkeypatch.setattr(
            planner_probe,
            "run_probe",
            lambda **kwargs: original(**{**kwargs, "timeout": 0.05}),
        )
    try:
        if mode in {"rate_limit", "auth", "timeout", "incompatible"}:
            with pytest.raises(ProbeError) as error:
                execute(tmp_path, spec)
            expected = {
                "rate_limit": "probe_rate_limited",
                "auth": "probe_authentication_failed",
                "timeout": "probe_timeout",
                "incompatible": "probe_protocol_incompatible",
            }[mode]
            assert controlled_failure_reason(spec, error.value) == expected
        else:
            result = execute(tmp_path, spec)
            assert result["curve"]["levels"][0]["decode_tps"] > 0
            assert result["curve"]["levels"][0]["usage_estimated"] == (mode == "missing_usage")
            assert "TEST-only" not in json.dumps(result)
        assert len(requests) == (2 if mode in {"fallback", "incompatible"} else 1)
        assert not (tmp_path / "credential.json").exists()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


async def test_real_docker_controlled_runtime_without_generation():
    import asyncio
    import os
    import uuid

    from autoeval.planning_transport import PlanningDocker
    from autoeval.resources import inspect_executor

    if os.getenv("AE_PLANNING_DOCKER_TEST") != "1":
        pytest.skip("Explicit Docker flag required")
    observed = inspect_executor("docker", "autoeval-demo-executor-1")
    adapter = PlanningDocker("docker", observed["container_id"], observed["image_id"])
    snapshot = await adapter.preflight()
    assert snapshot["cpu_cores"] > 0
    assert snapshot["mem_mb"] > 0
    assert snapshot["node_id"]
    key = str(uuid.uuid4())
    spec = {"kind": "framework_smoke", "duration_seconds": 0}
    job = await adapter.submit(spec, key)
    await adapter.submit(spec, key)
    for _ in range(20):
        status = await adapter.status(job)
        if status.state == "succeeded":
            break
        await asyncio.sleep(0.1)
    assert status.state == "succeeded"
    assert (await adapter.collect(job))["result"]["model_called"] is False
    assert (await adapter.cleanup(job))["cleaned"]
