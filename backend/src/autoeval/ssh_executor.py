"""Pinned SSH control and SFTP artifacts. No TOFU or arbitrary user commands."""

import asyncio
import base64
import hashlib
import hmac
import io
import json
import shlex
import stat
import uuid
from pathlib import Path, PurePosixPath

import paramiko
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from autoeval.contracts import JobObservation, JobRef


class SSHConnection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    host: str = Field(pattern=r"^[a-zA-Z0-9_.:-]{1,253}$")
    port: int = Field(default=22, ge=1, le=65535)
    username: str = Field(pattern=r"^[a-zA-Z_][a-zA-Z0-9_.-]{0,63}$")
    fingerprint: str = Field(pattern=r"^SHA256:[A-Za-z0-9+/]{43}$")
    workdir: str = Field(min_length=2, max_length=512)
    owner_id: uuid.UUID
    credential: SecretStr
    auth_kind: str = Field(pattern="^(password|private_key)$")

    @field_validator("workdir")
    @classmethod
    def absolute_directory(cls, value):
        path = PurePosixPath(value)
        if (
            not path.is_absolute()
            or path.parent == path
            or str(path) != value
            or ".." in path.parts
            or any(ord(c) < 32 for c in value)
        ):
            raise ValueError("Canonical absolute work directory required")
        return value


class PinnedHostKey(paramiko.MissingHostKeyPolicy):
    def __init__(self, fingerprint):
        self.fingerprint = fingerprint

    def missing_host_key(self, client, hostname, key):
        actual = "SHA256:" + base64.b64encode(
            hashlib.sha256(key.asbytes()).digest()
        ).decode().rstrip("=")
        if not hmac.compare_digest(actual, self.fingerprint):
            raise paramiko.SSHException("Host fingerprint differs from approved key")


def load_private_key(value):
    for key_type in (paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey):
        try:
            return key_type.from_private_key(io.StringIO(value))
        except (paramiko.SSHException, ValueError):
            continue
    raise ValueError("Unsupported, invalid or encrypted SSH private key")


class SSHExecutor:
    def __init__(self, connection: SSHConnection):
        self.connection = connection
        self.runtime = Path(__file__).with_name("job_runtime.py")
        self.digest = hashlib.sha256(self.runtime.read_bytes()).hexdigest()
        self.root = f"{connection.workdir}/.autoeval/{connection.owner_id}"
        self.remote_runtime = f"{self.root}/runtime/{self.digest}.py"

    def connect(self):
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(PinnedHostKey(self.connection.fingerprint))
        secret = self.connection.credential.get_secret_value()
        auth = (
            {"password": secret}
            if self.connection.auth_kind == "password"
            else {"pkey": load_private_key(secret)}
        )
        try:
            client.connect(
                hostname=self.connection.host,
                port=self.connection.port,
                username=self.connection.username,
                allow_agent=False,
                look_for_keys=False,
                timeout=5,
                banner_timeout=5,
                auth_timeout=8,
                channel_timeout=10,
                **auth,
            )
            return client
        except BaseException:
            client.close()
            raise

    @staticmethod
    def command(client, argv, payload=None):
        stdin, stdout, stderr = client.exec_command(shlex.join(argv), timeout=15)
        if payload is not None:
            stdin.write(payload)
            stdin.flush()
        stdin.channel.shutdown_write()
        raw = stdout.read(1024 * 1024 + 1)
        error = stderr.read(65537)
        code = stdout.channel.recv_exit_status()
        if code != 0 or len(raw) > 1024 * 1024 or len(error) > 65536:
            raise RuntimeError("Remote operation failed; inspect owned job evidence")
        return raw

    def prepare(self, client):
        # Reject symlink components before creating our own namespace. Never
        # recursively remove a configured workdir or another owner's directory.
        script = """from pathlib import Path
import sys
root=Path(sys.argv[1]); owner=sys.argv[2]
for p in reversed([root,*root.parents]):
 if p.is_symlink(): raise ValueError('Symlink in execution path')
root.mkdir(parents=True,exist_ok=True,mode=0o700)
marker=root/'.owner'
if marker.is_symlink(): raise ValueError('Invalid owner marker')
try:
 with marker.open('x') as f: f.write(owner)
except FileExistsError:
 if marker.read_text()!=owner: raise ValueError('Owner mismatch')
for name in ['runtime','jobs']:
 p=root/name
 if p.is_symlink(): raise ValueError('Invalid owned directory')
 p.mkdir(exist_ok=True,mode=0o700)
print('ready')
"""
        # Host staging must use the host interpreter, not a user PATH wrapper
        # which may enter a container and create files invisible to host SFTP.
        self.command(
            client, ["/usr/bin/python3", "-c", script, self.root, str(self.connection.owner_id)]
        )
        with client.open_sftp() as sftp:
            sftp.get_channel().settimeout(15)
            try:
                entry = sftp.lstat(self.remote_runtime)
            except FileNotFoundError:
                entry = None
            if entry is not None:
                if not stat.S_ISREG(entry.st_mode):
                    raise ValueError("Runtime is not a regular owned file")
                with sftp.open(self.remote_runtime, "rb") as stream:
                    raw = stream.read(len(self.runtime.read_bytes()) + 1)
                if hashlib.sha256(raw).hexdigest() != self.digest:
                    raise ValueError("Remote runtime checksum mismatch")
            else:
                with sftp.open(self.remote_runtime, "wx") as stream:
                    stream.write(self.runtime.read_bytes())
                sftp.chmod(self.remote_runtime, 0o600)

    def invoke(self, action, key, payload=None):
        key = str(uuid.UUID(key))
        with self.connect() as client:
            self.prepare(client)
            output = self.command(
                client,
                [
                    "env",
                    f"AE_JOB_ROOT={self.root}/jobs",
                    "python3",
                    self.remote_runtime,
                    action,
                    key,
                ],
                json.dumps(payload).encode() if payload is not None else None,
            )
            return json.loads(output)

    async def validate(self, spec):
        if (
            set(spec) != {"kind", "duration_seconds"}
            or spec["kind"] != "framework_smoke"
            or type(spec["duration_seconds"]) is not int
            or not 0 <= spec["duration_seconds"] <= 30
        ):
            raise ValueError("Only fixed framework_smoke is enabled for SSH")

    async def submit(self, spec, submission_key):
        await self.validate(spec)
        key = str(uuid.UUID(submission_key))
        await asyncio.to_thread(self.invoke, "submit", key, spec)
        return JobRef(external_job_id=key, submission_key=key)

    async def lookup(self, submission_key):
        job = JobRef(external_job_id=submission_key, submission_key=submission_key)
        return await self.status(job)

    async def status(self, job):
        raw = await asyncio.to_thread(self.invoke, "status", job.submission_key)
        return JobObservation(job=job, state=raw["state"], reason=raw.get("reason"))

    async def cancel(self, job, operation_key):
        raw = await asyncio.to_thread(self.invoke, "cancel", job.submission_key)
        return JobObservation(job=job, state=raw["state"], reason=raw.get("reason"))

    async def collect(self, job):
        # Result collection uses SFTP rather than scraping terminal output.
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
                            str(uuid.UUID(job.submission_key)),
                        ],
                    )
                )
                if state["state"] == "succeeded":
                    path = f"{self.root}/jobs/{uuid.UUID(job.submission_key)}/result.json"
                    with client.open_sftp() as sftp:
                        sftp.get_channel().settimeout(15)
                        if not stat.S_ISREG(sftp.lstat(path).st_mode):
                            raise ValueError("Result is not a regular file")
                        with sftp.open(path, "rb") as stream:
                            raw = stream.read(1024 * 1024 + 1)
                        if len(raw) > 1024 * 1024:
                            raise ValueError("Result size limit exceeded")
                        state["result"] = json.loads(raw)
                return state

        return await asyncio.to_thread(collect)

    async def cleanup(self, job):
        return await asyncio.to_thread(self.invoke, "cleanup", job.submission_key)
