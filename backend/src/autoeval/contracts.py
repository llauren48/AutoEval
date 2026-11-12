from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Capabilities(Contract):
    provider_kind: str
    direct_runner: bool = False
    delegated_benchmark: bool = False
    cancellation: bool = False
    result_export: bool = False


class JobRef(Contract):
    external_job_id: str
    submission_key: str


class JobObservation(Contract):
    job: JobRef
    state: Literal[
        "queued", "running", "succeeded", "failed", "cancelled", "unknown", "cleaned"
    ]
    reason: str | None = None


class ResourceProvider(Protocol):
    async def capabilities(self) -> Capabilities: ...
    async def validate(self, binding: dict[str, Any]) -> dict[str, Any]: ...
    async def acquire(self, binding: dict[str, Any], operation_key: str) -> dict[str, Any]: ...
    async def inspect(self, allocation: dict[str, Any]) -> dict[str, Any]: ...
    async def release(self, allocation: dict[str, Any], operation_key: str) -> dict[str, Any]: ...


class ExecutorAdapter(Protocol):
    async def validate(self, spec: dict[str, Any]) -> None: ...
    async def submit(self, spec: dict[str, Any], submission_key: str) -> JobRef: ...
    async def lookup(self, submission_key: str) -> JobObservation: ...
    async def status(self, job: JobRef) -> JobObservation: ...
    async def cancel(self, job: JobRef, operation_key: str) -> JobObservation: ...
    async def collect(self, job: JobRef) -> dict[str, Any]: ...
    async def cleanup(self, job: JobRef) -> dict[str, Any]: ...
