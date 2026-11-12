"""Controlled executor extension; legacy diagnostic transports keep their contract."""

import asyncio
import hashlib
import json
import stat
from pathlib import Path

from autoeval.docker_executor import DockerExecutor
from autoeval.planning_runtime import validate_spec
from autoeval.resources import ResourceBinding
from autoeval.ssh_executor import SSHExecutor
from autoeval.ssh_resources import SSHResourceBinding, connection_for

RUNTIME = Path(__file__).with_name("planning_runtime.py")


class PlanningDocker(DockerExecutor):
    def __init__(self, executable, container_id, image_id):
        super().__init__(executable, container_id, image_id)
        self.remote_dir = (
            "/work/autoeval-runtime/"
            + hashlib.sha256(self.runtime.read_bytes() + RUNTIME.read_bytes()).hexdigest()
        )

    def command(self, arguments, payload=None):
        import subprocess

        # Structured results are bounded independently from the diagnostic transport.
        result = subprocess.run(
            [self.executable, *arguments],
            input=payload,
            capture_output=True,
            timeout=30,
            check=True,
        )
        if len(result.stdout) > 16 * 1024 * 1024:
            raise ValueError("Controlled response too large")
        return result.stdout

    async def validate(self, spec):
        if spec.get("kind") != "framework_smoke":
            validate_spec(spec)
        await super().validate({"kind": "framework_smoke", "duration_seconds": 0})

    async def install_runtime(self):
        await super().install_runtime()
        await asyncio.to_thread(
            self.command,
            ["cp", str(RUNTIME), f"{self.container_id}:{self.remote_dir}/planning_runtime.py"],
        )

    async def preflight(self, tasks=None):
        await self.validate({"kind": "framework_smoke", "duration_seconds": 0})
        await self.install_runtime()
        return json.loads(
            await asyncio.to_thread(
                self.command,
                [
                    "exec",
                    "-i",
                    self.container_id,
                    "python",
                    self.remote_dir + "/planning_runtime.py",
                    "preflight",
                ],
                json.dumps(tasks).encode(),
            )
        )


class PlanningSSH(SSHExecutor):
    def __init__(self, connection):
        super().__init__(connection)
        combined = hashlib.sha256(self.runtime.read_bytes() + RUNTIME.read_bytes()).hexdigest()
        self.remote_runtime = f"{self.root}/runtime/{combined}/job_runtime.py"

    def prepare(self, client):
        # Parent installs the legacy runtime with checksum validation in its own namespace.
        controlled = self.remote_runtime
        self.remote_runtime = f"{self.root}/runtime/{self.digest}.py"
        try:
            super().prepare(client)
        finally:
            self.remote_runtime = controlled
        folder = str(Path(controlled).parent).replace("\\", "/")
        self.command(
            client,
            [
                "python3",
                "-c",
                "import pathlib,sys; p=pathlib.Path(sys.argv[1]); assert not p.is_symlink(); p.mkdir(mode=0o700,exist_ok=True)",
                folder,
            ],
        )
        with client.open_sftp() as sftp:
            for source in (self.runtime, RUNTIME):
                remote = folder + "/" + source.name
                try:
                    entry = sftp.lstat(remote)
                except FileNotFoundError:
                    with sftp.open(remote, "wx") as stream:
                        stream.write(source.read_bytes())
                    sftp.chmod(remote, 0o600)
                else:
                    if not stat.S_ISREG(entry.st_mode):
                        raise ValueError("Runtime must be a regular file")
                    with sftp.open(remote, "rb") as stream:
                        if stream.read(len(source.read_bytes()) + 1) != source.read_bytes():
                            raise ValueError("Controlled runtime checksum mismatch")

    async def validate(self, spec):
        validate_spec(spec)

    async def preflight(self, tasks=None):
        def check():
            with self.connect() as client:
                self.prepare(client)
                path = self.remote_runtime.rsplit("/", 1)[0] + "/planning_runtime.py"
                return json.loads(
                    self.command(
                        client,
                        ["python3", path, "preflight"],
                        json.dumps(tasks).encode(),
                    )
                )

        return await asyncio.to_thread(check)

    async def collect(self, job):
        # General results can be larger than the 1 MB diagnostic result limit.
        def collect():
            with self.connect() as client:
                self.prepare(client)
                state = json.loads(
                    self.command(
                        client,
                        [
                            "env",
                            f"AE_JOB_ROOT={self.root}/jobs",
                            "python3",
                            self.remote_runtime,
                            "status",
                            job.submission_key,
                        ],
                    )
                )
                if state["state"] == "succeeded":
                    path = f"{self.root}/jobs/{job.submission_key}/result.json"
                    with client.open_sftp() as sftp:
                        if not stat.S_ISREG(sftp.lstat(path).st_mode):
                            raise ValueError("Result must be a regular file")
                        with sftp.open(path, "rb") as stream:
                            raw = stream.read(16 * 1024 * 1024 + 1)
                        if len(raw) > 16 * 1024 * 1024:
                            raise ValueError("Result too large")
                        state["result"] = json.loads(raw)
                return state

        return await asyncio.to_thread(collect)


async def transport(session, settings, inputs):
    if inputs["resource_kind"] == "customer_ssh":
        row = await session.get(SSHResourceBinding, inputs["resource_id"])
        if row is None:
            raise ValueError("SSH binding missing")
        return PlanningSSH(connection_for(row, settings))
    row = await session.get(ResourceBinding, inputs["resource_id"])
    if row is None:
        raise ValueError("Docker binding missing")
    return PlanningDocker(settings.docker_executable, row.container_id, row.image_id)
