"""Admin review workflow, exclusive review leases, and QA decisions."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging
import uuid
from typing import List, Optional, Tuple

from schemas import (
    RejectionHistoryItem,
    ReviewLease,
    ReviewRecord,
    ReviewStatus,
    TaskState,
    TaskStatus,
    utc_now_iso,
)
from services.storage import (
    StorageBackend,
    StoragePreconditionFailedError,
)
from services.tasks import _state_key, get_task_state

logger = logging.getLogger(__name__)

DEFAULT_REVIEW_LEASE_TTL = 300  # 5 minutes


def claim_review_lease(
    storage: StorageBackend,
    dataset_name: str,
    task_id: str,
    reviewer_id: str,
    reviewer_name: Optional[str] = None,
    ttl_seconds: int = DEFAULT_REVIEW_LEASE_TTL,
) -> Tuple[bool, str, Optional[ReviewLease]]:
    st, etag = get_task_state(storage, dataset_name, task_id)
    if not st:
        return False, "Không tìm thấy trạng thái câu hỏi.", None

    if st.status != TaskStatus.RECORDED or not st.current_audio:
        return False, "Câu này chưa có bản ghi âm để kiểm tra.", None

    now = datetime.now(timezone.utc)
    if st.review_lease and not st.review_lease.is_expired(now):
        if st.review_lease.reviewer_id != reviewer_id:
            holder = st.review_lease.reviewer_name or st.review_lease.reviewer_id
            return (
                False,
                f"Câu này đang được kiểm duyệt bởi quản trị viên '{holder}'. Vui lòng thử lại sau.",
                None,
            )
        lease_id = st.review_lease.lease_id
    else:
        lease_id = str(uuid.uuid4())

    expires_dt = now + timedelta(seconds=ttl_seconds)
    lease = ReviewLease(
        lease_id=lease_id,
        reviewer_id=reviewer_id,
        reviewer_name=reviewer_name,
        created_at=now.isoformat(),
        expires_at=expires_dt.isoformat(),
    )

    st.review_lease = lease
    st.updated_at = utc_now_iso()

    key = _state_key(dataset_name, task_id)
    state_bytes = json.dumps(st.model_dump(), ensure_ascii=False, indent=2).encode("utf-8")

    try:
        storage.put_object_if_match(
            key, state_bytes, etag, content_type="application/json"
        )
        return True, "Nhận quyền kiểm duyệt thành công.", lease
    except StoragePreconditionFailedError:
        return False, "Xảy ra xung đột: trạng thái đã được cập nhật bởi quản trị viên khác.", None


def release_review_lease(
    storage: StorageBackend,
    dataset_name: str,
    task_id: str,
    review_lease_id: str,
) -> bool:
    st, etag = get_task_state(storage, dataset_name, task_id)
    if not st or not st.review_lease:
        return True

    if st.review_lease.lease_id != review_lease_id:
        return False

    st.review_lease = None
    st.updated_at = utc_now_iso()

    key = _state_key(dataset_name, task_id)
    state_bytes = json.dumps(st.model_dump(), ensure_ascii=False, indent=2).encode("utf-8")

    try:
        storage.put_object_if_match(
            key, state_bytes, etag, content_type="application/json"
        )
        return True
    except StoragePreconditionFailedError:
        return False


def approve_recording(
    storage: StorageBackend,
    dataset_name: str,
    task_id: str,
    review_lease_id: str,
    reviewer_id: str,
    reviewer_name: Optional[str] = None,
    verified_transcript: Optional[str] = None,
    expected_recording_id: Optional[str] = None,
) -> Tuple[bool, str]:
    st, etag = get_task_state(storage, dataset_name, task_id)
    if not st or st.status != TaskStatus.RECORDED or not st.current_audio:
        return False, "Câu không tồn tại hoặc chưa có bản ghi."

    if expected_recording_id and st.current_audio.recording_id != expected_recording_id:
        return False, "Bản ghi đã bị thay đổi hoặc thu lại bởi phiên khác."

    now = datetime.now(timezone.utc)
    if not st.review_lease or st.review_lease.lease_id != review_lease_id or st.review_lease.is_expired(now):
        return False, "Phiên kiểm duyệt đã hết hạn hoặc không hợp lệ. Vui lòng tải lại trang."

    is_edited = bool(verified_transcript and verified_transcript.strip())
    new_status = ReviewStatus.APPROVED_EDITED if is_edited else ReviewStatus.APPROVED

    st.review = ReviewRecord(
        status=new_status,
        verified_transcript=verified_transcript.strip() if is_edited else None,
        reviewer_id=reviewer_id,
        reviewer_name=reviewer_name,
        updated_at=utc_now_iso(),
    )
    st.review_lease = None
    st.updated_at = utc_now_iso()

    key = _state_key(dataset_name, task_id)
    state_bytes = json.dumps(st.model_dump(), ensure_ascii=False, indent=2).encode("utf-8")

    try:
        storage.put_object_if_match(
            key, state_bytes, etag, content_type="application/json"
        )
        msg = "Đã duyệt bản ghi (sửa văn bản)" if is_edited else "Đã duyệt bản ghi thành công."
        return True, msg
    except StoragePreconditionFailedError:
        return False, "Xung đột ghi: Trạng thái câu đã bị thay đổi bởi quản trị viên khác."


def reject_recording(
    storage: StorageBackend,
    dataset_name: str,
    task_id: str,
    review_lease_id: str,
    reviewer_id: str,
    reviewer_name: Optional[str] = None,
    error_reasons: Optional[List[str]] = None,
    note: Optional[str] = None,
    expected_recording_id: Optional[str] = None,
) -> Tuple[bool, str]:
    st, etag = get_task_state(storage, dataset_name, task_id)
    if not st or st.status != TaskStatus.RECORDED or not st.current_audio:
        return False, "Câu không tồn tại hoặc chưa có bản ghi."

    if expected_recording_id and st.current_audio.recording_id != expected_recording_id:
        return False, "Bản ghi đã bị thay đổi bởi phiên khác."

    now = datetime.now(timezone.utc)
    if not st.review_lease or st.review_lease.lease_id != review_lease_id or st.review_lease.is_expired(now):
        return False, "Phiên kiểm duyệt đã hết hạn hoặc không hợp lệ."

    reasons = error_reasons or []

    history_item = RejectionHistoryItem(
        recording_id=st.current_audio.recording_id,
        object_key=st.current_audio.object_key,
        speaker_id=st.current_audio.speaker_id,
        speaker_name=st.current_audio.speaker_name,
        rejected_at=utc_now_iso(),
        reviewer_id=reviewer_id,
        reviewer_name=reviewer_name,
        error_reasons=reasons,
        note=note.strip() if note else None,
    )
    st.rejection_history.append(history_item)

    st.status = TaskStatus.AVAILABLE
    st.recording_lease = None
    st.review_lease = None
    st.review = ReviewRecord(
        status=ReviewStatus.REJECTED,
        reviewer_id=reviewer_id,
        reviewer_name=reviewer_name,
        error_reasons=reasons,
        note=note.strip() if note else None,
        updated_at=utc_now_iso(),
    )
    st.updated_at = utc_now_iso()

    key = _state_key(dataset_name, task_id)
    state_bytes = json.dumps(st.model_dump(), ensure_ascii=False, indent=2).encode("utf-8")

    try:
        storage.put_object_if_match(
            key, state_bytes, etag, content_type="application/json"
        )
        return True, "Đã từ chối bản ghi và mở lại câu cho cộng tác viên thu lại."
    except StoragePreconditionFailedError:
        return False, "Xung đột ghi: Trạng thái câu đã bị thay đổi bởi thao tác khác."
