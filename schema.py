"""Pydantic schemas for API request/response validation."""

from datetime import date, datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict


# --- Batch Schemas ---


class BatchCreate(BaseModel):
    """Schema for creating a batch."""

    batch_name: str
    max_parallel_calls: int | None = None
    refill_threshold: int | None = None
    max_attempts: int | None = None


class BatchResponse(BaseModel):
    """Schema for batch response."""

    model_config = ConfigDict(from_attributes=True)

    batch_name: str
    is_active: bool
    max_parallel_calls: int
    refill_threshold: int
    max_attempts: int
    created_at: datetime
    updated_at: datetime


class BatchStartResponse(BaseModel):
    """Response for batch start endpoint."""

    batch_name: str
    started: bool
    message: str


class BatchStopResponse(BaseModel):
    """Response for batch stop endpoint."""

    batch_name: str
    stopped: bool
    message: str


class BatchNamesResponse(BaseModel):
    """Response for batch names list endpoint."""

    batch_names: list[str]


class BatchNameAvailabilityResponse(BaseModel):
    """Response for batch name availability check."""

    batch_name: str
    available: bool


class BatchResetResponse(BaseModel):
    """Response for batch reset endpoint."""

    batch_name: str
    reset_count: int
    message: str


# --- Lead Schemas ---


class LeadResponse(BaseModel):
    """Schema for lead response."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    batch_name: str
    row_order: int
    phone: str
    name: str | None
    extra_data: dict[str, Any] | None
    attempt_count: int
    status: int | None
    redial_date: date | None
    ready_for_call: bool
    inflight: bool
    inflight_room_id: str | None
    last_call_at: datetime | None
    # Call outcome details
    model: str | None = None
    variant: str | None = None
    time_to_buy: str | None = None  # 30_days, 60_days, 90_days
    dealer_code: str | None = None
    created_at: datetime
    updated_at: datetime


# --- Upload Schemas ---


class UploadResponse(BaseModel):
    """Response for CSV upload endpoint."""

    batch_name: str
    inserted_count: int
    skipped_count: int
    message: str


# --- Stats Schemas ---


class StatsResponse(BaseModel):
    """Response for batch stats endpoint."""

    batch_name: str
    is_active: bool
    total_leads: int
    not_called: int  # status IS NULL
    success: int  # status = 1
    not_interested: int  # status = -1
    recall_pending: int  # status = 0
    inflight: int
    eligible_now: int  # ready to be called now
    max_attempts_reached: int


# --- Call Log Schemas ---


class TranscriptEntry(BaseModel):
    """Single entry in a call transcript."""

    role: str  # "user" or "assistant"
    text: str
    timestamp: datetime | None = None


class CallLogResponse(BaseModel):
    """Schema for call log response."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    lead_id: UUID
    room_id: str | None
    started_at: datetime
    ended_at: datetime | None
    outcome: str | None
    agent_summary: str | None
    transcript: list[TranscriptEntry] | None = None
