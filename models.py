"""SQLAlchemy models for the dialer orchestrator."""

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Base class for all models."""

    pass


class Batch(Base):
    """Batch of leads for outbound calling."""

    __tablename__ = "batches"

    batch_name: Mapped[str] = mapped_column(String(255), primary_key=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    max_parallel_calls: Mapped[int] = mapped_column(Integer, default=10)
    refill_threshold: Mapped[int] = mapped_column(Integer, default=7)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationship to leads
    leads: Mapped[list["Lead"]] = relationship("Lead", back_populates="batch")


class Lead(Base):
    """Individual lead/prospect for calling."""

    __tablename__ = "leads"
    __table_args__ = (
        UniqueConstraint("batch_name", "phone", name="uq_batch_phone"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    batch_name: Mapped[str] = mapped_column(
        String(255), ForeignKey("batches.batch_name"), nullable=False
    )
    row_order: Mapped[int] = mapped_column(Integer, nullable=False)

    # Contact info
    phone: Mapped[str] = mapped_column(String(50), nullable=False)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    extra_data: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    # Call tracking
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )  # NULL=not called, 0=recall, 1=success, -1=not interested
    redial_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    ready_for_call: Mapped[bool] = mapped_column(Boolean, default=True)

    # Inflight tracking
    inflight: Mapped[bool] = mapped_column(Boolean, default=False)
    inflight_room_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_call_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Call outcome details (updated after call)
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)  # Car model
    variant: Mapped[str | None] = mapped_column(String(100), nullable=True)  # Car variant
    time_to_buy: Mapped[str | None] = mapped_column(
        String(20), nullable=True
    )  # 30_days, 60_days, 90_days
    dealer_code: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )  # Dealer name/code

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    batch: Mapped["Batch"] = relationship("Batch", back_populates="leads")
    call_logs: Mapped[list["CallLog"]] = relationship("CallLog", back_populates="lead")


class CallLog(Base):
    """Log of individual call attempts."""

    __tablename__ = "call_logs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    lead_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leads.id"), nullable=False
    )
    room_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Call timing
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Outcome
    outcome: Mapped[str | None] = mapped_column(
        String(50), nullable=True
    )  # connected, not_connected, voicemail, etc.
    agent_summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Transcript - stores conversation as list of {role, text, timestamp}
    transcript: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)

    # Additional data
    extra_data: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    # Relationship
    lead: Mapped["Lead"] = relationship("Lead", back_populates="call_logs")
