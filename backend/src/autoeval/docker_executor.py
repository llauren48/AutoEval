"""Controlled Docker job adapter, initially supporting framework_smoke only."""

import asyncio
import hashlib
import json
import subprocess
import uuid
from pathlib import Path

from autoeval.contracts import JobObservation, JobRef
from autoeval.resources import inspect_executor


class DockerExecutor:
    def __init__(self, executable: str, container_id: str, image_id: str):
        self.executable, self.container_id, self.image_id = executable, container_id, image_id
        self.runtime = Path(__file__).with_name("job_runtime.py")
        self.remote_dir = (
            "/work/autoeval-runtime/" + hashlib.sha256(self.runtime.read_bytes()).hexdigest()
        )

    async def validate(self, spec: dict) -> None:
        if set(spec) != {"kind", "duration_seconds"} or spec["kind"] != "framework_smoke":
            raise ValueError("Tool integration not configured; only framework_smoke is supported")
        if type(spec["duration_seconds"]) is not int or not 0 <= spec["duration_seconds"] <= 30:
            raise ValueError("Invalid smoke duration")
        current = await asyncio.to_thread(inspect_executor, self.executable, self.container_id)
        if not current["running"] or current["image_id"] != self.image_id:
            raise ValueError("Executor identity changed or unavailable")

    def command(self, arguments, payload=None):
        result = subprocess.run(
            [self.executable, *arguments],
            input=payload,
            capture_output=True,
            timeout=15,
            check=True,
        )
        return result.stdout

    async def install_runtime(self):
        await asyncio.to_thread(
            self.command,
            [
                "exec",
                self.container_id,
                "python",
                "-c",
                "from pathlib import Path; import sys; Path(sys.argv[1]).mkdir(parents=True,exist_ok=True)",
                self.remote_dir,
            ],
        )
        await asyncio.to_thread(
            self.command,
            ["cp", str(self.runtime), f"{self.container_id}:{self.remote_dir}/job_runtime.py"],
        )

    async def _invoke(self, action, key, payload=None):
        key = str(uuid.UUID(key))
        await self.validate({"kind": "framework_smoke", "duration_seconds": 0})
        output = await asyncio.to_thread(
            self.command,
            [
                "exec",
                "-i",
                self.container_id,
                "python",
                self.remote_dir + "/job_runtime.py",
                action,
                key,
            ],
            json.dumps(payload).encode() if payload is not None else None,
        )
        return json.loads(output)

    async def submit(self, spec: dict, submission_key: str) -> JobRef:
        await self.validate(spec)
        await self.install_runtime()
        key = str(uuid.UUID(submission_key))
        await self._invoke("submit", key, spec)
        return JobRef(external_job_id=key, submission_key=key)

    async def lookup(self, submission_key: str) -> JobObservation:
        await self.install_runtime()
        job = JobRef(external_job_id=submission_key, submission_key=submission_key)
        result = await self._invoke("status", submission_key)
        return JobObservation(job=job, state=result["state"], reason=result.get("reason"))

    async def status(self, job: JobRef) -> JobObservation:
        return await self.lookup(job.submission_key)

    async def cancel(self, job: JobRef, operation_key: str) -> JobObservation:
        result = await self._invoke("cancel", job.submission_key)
        return JobObservation(job=job, state=result["state"], reason=result.get("reason"))

    async def collect(self, job: JobRef) -> dict:
        return await self._invoke("collect", job.submission_key)

    async def cleanup(self, job: JobRef) -> dict:
        return await self._invoke("cleanup", job.submission_key)
