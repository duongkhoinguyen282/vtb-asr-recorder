"""Data models and schemas for the VietinBank Vietnamese ASR recording application."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any, List, Optional
from pydantic import BaseModel, Field, field_validator


def utc_now_iso() -> str:
    """Return current UTC timestamp in ISO 8601 format."""
    return datetime.now(timezone.utc).isoformat()


class VoiceGender(str, Enum):
    MALE = "male"
    FEMALE = "female"


class VoiceEligibility(str, Enum):
    MALE = "male"
    FEMALE = "female"
    ANY = "any"


class Accent(str, Enum):
    NORTH = "bac"
    CENTRAL = "trung"
    SOUTH = "nam"
    OTHER = "khac"


class TaskStatus(str, Enum):
    AVAILABLE = "available"
    RESERVED = "reserved"
    RECORDED = "recorded"


class ReviewStatus(str, Enum):
    UNCHECKED = "unchecked"
    APPROVED = "approved"
    APPROVED_EDITED = "approved_edited"
    REJECTED = "rejected"


SAFE_SLUG_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")


class ManifestItem(BaseModel):
    """Canonical representation of a recording script item."""

    id: str = Field(..., description="Unique task identifier, e.g. HNUM-0001")
    reading_script: str = Field(..., description="Non-empty Vietnamese text to speak")
    delivery_instruction: str = Field(
        ..., description="Delivery instructions; never spoken"
    )
    domain: str = Field(..., description="Domain category, e.g. cccd_12, phone, bank_account")
    scenario: str = Field(
        ..., description="Speech disfluency scenario, e.g. restart, correction, chunk_repeat"
    )
    speaker_voice_eligibility: VoiceEligibility = Field(
        ..., description="Hard restriction: male, female, or any"
    )
    planned_voice_gender: VoiceGender = Field(
        ..., description="Soft routing preference: male or female"
    )

    pair_id: Optional[str] = None
    number_pattern: Optional[str] = None
    target_digits: Optional[str] = None
    verbatim_reference_draft: Optional[str] = None
    gold_transcript: Optional[str] = None
    tags: Optional[List[str]] = None
    agent: Optional[str] = None
    source: Optional[str] = None
    review_status: Optional[str] = None
    gender_routing_basis: Optional[str] = None
    extra_metadata: Optional[dict[str, Any]] = None

    @field_validator("id")
    @classmethod
    def validate_id(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Task ID cannot be empty")
        if not SAFE_SLUG_PATTERN.match(v):
            raise ValueError(
                f"Task ID '{v}' contains invalid characters. Use alphanumeric, dash, or underscore."
            )
        return v

    @field_validator("reading_script", "delivery_instruction", "domain", "scenario")
    @classmethod
    def validate_non_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Required text field cannot be empty")
        return v

    def is_eligible(self, speaker_gender: str) -> bool:
        """Check if a speaker gender is eligible for this task."""
        gender_str = speaker_gender.lower()
        if self.speaker_voice_eligibility == VoiceEligibility.ANY:
            return True
        return self.speaker_voice_eligibility.value == gender_str


class RecordingLease(BaseModel):
    """Temporary reservation lease on a task."""

    lease_id: str
    session_id: str
    speaker_name: Optional[str] = None
    speaker_gender: str
    accent: Optional[str] = None
    created_at: str = Field(default_factory=utc_now_iso)
    expires_at: str

    def is_expired(self, now: Optional[datetime] = None) -> bool:
        current = now or datetime.now(timezone.utc)
        try:
            exp = datetime.fromisoformat(self.expires_at)
            return current >= exp
        except Exception:
            return True


class AudioMetadata(BaseModel):
    """Metadata of an uploaded audio recording."""

    recording_id: str
    object_key: str
    speaker_id: str
    speaker_name: Optional[str] = None
    voice_gender: str
    accent: Optional[str] = None
    sha256: str
    duration_seconds: float
    sample_rate: int = 16000
    channels: int = 1
    uploaded_at: str = Field(default_factory=utc_now_iso)


class ReviewRecord(BaseModel):
    """Review decision and notes for a recording."""

    status: ReviewStatus = ReviewStatus.UNCHECKED
    verified_transcript: Optional[str] = None
    reviewer_id: Optional[str] = None
    reviewer_name: Optional[str] = None
    note: Optional[str] = None
    error_reasons: List[str] = Field(default_factory=list)
    updated_at: Optional[str] = None


class ReviewLease(BaseModel):
    """Short-lived exclusive lock for an admin reviewer."""

    lease_id: str
    reviewer_id: str
    reviewer_name: Optional[str] = None
    created_at: str = Field(default_factory=utc_now_iso)
    expires_at: str

    def is_expired(self, now: Optional[datetime] = None) -> bool:
        current = now or datetime.now(timezone.utc)
        try:
            exp = datetime.fromisoformat(self.expires_at)
            return current >= exp
        except Exception:
            return True


class RejectionHistoryItem(BaseModel):
    """Historical audit record of a rejection."""

    recording_id: str
    object_key: str
    speaker_id: Optional[str] = None
    speaker_name: Optional[str] = None
    rejected_at: str
    reviewer_id: Optional[str] = None
    reviewer_name: Optional[str] = None
    error_reasons: List[str] = Field(default_factory=list)
    note: Optional[str] = None


class TaskState(BaseModel):
    """Authoritative task state stored in remote S3 bucket."""

    task_id: str
    status: TaskStatus = TaskStatus.AVAILABLE
    recording_lease: Optional[RecordingLease] = None
    current_audio: Optional[AudioMetadata] = None
    review: ReviewRecord = Field(default_factory=ReviewRecord)
    review_lease: Optional[ReviewLease] = None
    rejection_history: List[RejectionHistoryItem] = Field(default_factory=list)
    updated_at: str = Field(default_factory=utc_now_iso)

    def is_currently_available(self, now: Optional[datetime] = None) -> bool:
        if self.status == TaskStatus.RECORDED:
            return False
        if self.status == TaskStatus.AVAILABLE:
            return True
        if self.status == TaskStatus.RESERVED:
            return self.recording_lease is None or self.recording_lease.is_expired(now)
        return False


class DatasetConfig(BaseModel):
    """Dataset configuration and metadata."""

    name: str = Field(..., description="Dataset slug, e.g. numeric-data-v1")
    display_name: str
    task_count: int
    created_at: str = Field(default_factory=utc_now_iso)
    description: Optional[str] = None


class DatasetsRegistry(BaseModel):
    """Registry of datasets stored in datasets.json at the bucket root."""

    active_dataset: str
    datasets: dict[str, DatasetConfig] = Field(default_factory=dict)
    updated_at: str = Field(default_factory=utc_now_iso)
