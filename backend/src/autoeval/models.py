import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Record:
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EvaluationRun(Record, Base):
    __tablename__ = "evaluation_run"
    trigger_scope: Mapped[str] = mapped_column(String(256))
    idempotency_key: Mapped[str] = mapped_column(String(256))
    request_hash: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(24), server_default="accepted")
    run_mode: Mapped[str] = mapped_column(String(24))
    created_by: Mapped[str] = mapped_column(String(256))
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    projection_version: Mapped[int] = mapped_column(BigInteger, server_default="1")
    __table_args__ = (
        UniqueConstraint("trigger_scope", "idempotency_key"),
        CheckConstraint("run_mode IN ('exploration','release_validation')"),
        CheckConstraint(
            "state IN ('accepted','queued','preparing','running','aggregating',"
            "'completed','cancelling','cancelled','failed')"
        ),
        Index("ix_run_created", "created_at", "id"),
    )


class RunSnapshot(Record, Base):
    __tablename__ = "run_snapshot"
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("evaluation_run.id", ondelete="RESTRICT"), unique=True
    )
    schema_version: Mapped[str] = mapped_column(String(16))
    canonical_body: Mapped[dict] = mapped_column(JSONB)
    sha256: Mapped[str] = mapped_column(String(64))


class RunEvent(Record, Base):
    __tablename__ = "run_event"
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("evaluation_run.id", ondelete="RESTRICT"))
    seq: Mapped[int] = mapped_column(BigInteger)
    type: Mapped[str] = mapped_column(String(64))
    body: Mapped[dict] = mapped_column(JSONB)
    __table_args__ = (UniqueConstraint("run_id", "seq"), CheckConstraint("seq > 0"))


class Outbox(Record, Base):
    __tablename__ = "outbox"
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("evaluation_run.id", ondelete="RESTRICT"))
    kind: Mapped[str] = mapped_column(String(32))
    dedup_key: Mapped[str] = mapped_column(String(256), unique=True)
    payload: Mapped[dict] = mapped_column(JSONB)
    state: Mapped[str] = mapped_column(String(24), server_default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, server_default="0")
    next_retry_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    __table_args__ = (
        CheckConstraint("kind IN ('START_RUN','CANCEL_RUN','ANALYZE_REPORT')"),
        CheckConstraint("state IN ('pending','delivered')"),
        CheckConstraint("attempt_count >= 0"),
        Index("ix_outbox_pending", "next_retry_at", postgresql_where=text("state = 'pending'")),
    )
