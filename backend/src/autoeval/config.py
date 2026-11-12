import uuid
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AE_", env_file=".env", extra="ignore")
    environment: Literal["development", "test", "production"] = "development"
    auth_mode: Literal["local"] = "local"
    database_url: SecretStr = SecretStr(
        "postgresql+asyncpg://autoeval:autoeval@127.0.0.1:5432/autoeval"
    )
    temporal_address: str = "127.0.0.1:7233"
    temporal_namespace: str = "default"
    s3_endpoint: str = "http://127.0.0.1:9000"
    s3_bucket: str = "autoeval-evidence"
    s3_access_key: SecretStr = SecretStr("local-development")
    s3_secret_key: SecretStr = SecretStr("local-development-only")
    dependency_timeout_seconds: float = 3.0
    poll_seconds: float = 5.0
    secret_store_path: Path = Path(".secrets")
    unified_fixture_config: Path | None = None
    aisbench_lock_path: Path = Path("tool-locks/aisbench.json")
    gpqa_activity_queue: str = "demo.gpqa.v1"
    gpqa_workflow_queue: str = "demo.workflow.v1"
    analysis_target_id: uuid.UUID | None = None
    analysis_resource_id: uuid.UUID | None = None
    analysis_activity_queue: str = "demo.result.v1"
    analysis_workflow_queue: str = "demo.workflow.v1"
    analysis_max_output_tokens: int = Field(default=2048, ge=1, le=4096)
    evaluation_origin: Literal["real", "simulation"] = "real"
    allowed_model_hosts: list[str] = ["api.deepseek.com"]
    docker_executable: str = "docker"

    @model_validator(mode="after")
    def local_only(self):
        if self.environment == "production":
            raise ValueError(
                "Production identity is not implemented; local auth is development only"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
