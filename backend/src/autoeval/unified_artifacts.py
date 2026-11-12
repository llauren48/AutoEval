"""Append-only output references, separate from immutable input snapshots."""

import uuid

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from autoeval.models import Base, Record


class UnifiedArtifact(Record, Base):
    __tablename__ = "unified_output_artifact"
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("evaluation_run.id", ondelete="RESTRICT"))
    collection_key: Mapped[str] = mapped_column(String(128))
    object_key: Mapped[str] = mapped_column(String(256))
    sha256: Mapped[str] = mapped_column(String(64))
    manifest: Mapped[dict] = mapped_column(JSONB)
    __table_args__ = (UniqueConstraint("run_id", "collection_key"),)
