"""Task lifecycle management, concurrent-safe reservations, and audio submission."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import uuid
from typing import Any, Dict, List, Optional, Tuple

from schemas import (
    AudioMetadata,
    ManifestItem,
    RecordingLease,
    ReviewRecord,
    ReviewStatus,
    TaskState,
    TaskStatus,
    utc_now_iso,
)
from services.audio import AudioValidationError, validate_and_process_audio
from services.storage import (
    StorageBackend,
    StorageNotFoundError,
    StoragePreconditionFailedError,
)

logger = logging.getLogger(__name__)

DEFAULT_LEASE_TTL_SECONDS = 900  # 15 minutes


def _state_key(dataset_name: str, task_id: str) -> str:
    return f"{dataset_name}/states/{task_id}.json"


def get_task_state(
    storage: StorageBackend, dataset_name: str, task_id: str
) -> Tuple[Optional[TaskState], Optional[str]]:
    key = _state_key(dataset_name, task_id)
    try:
        data, etag = storage.get_object(key)
        state_data = json.loads(data.decode("utf-8"))
        return TaskState(**state_data), etag
    except StorageNotFoundError:
        return None, None


def load_all_task_states(
    storage: StorageBackend, dataset_name: str
) -> Dict[str, Tuple[TaskState, str]]:
    prefix = f"{dataset_name}/states/"
    objects = storage.list_objects(prefix=prefix)
    states: Dict[str, Tuple[TaskState, str]] = {}
    for item in objects:
        key = item["Key"]
        if not key.endswith(".json"):
            continue
        try:
            data, etag = storage.get_object(key)
            state_data = json.loads(data.decode("utf-8"))
            st = TaskState(**state_data)
            states[st.task_id] = (st, etag)
        except Exception as exc:
            logger.warning("Error reading state %s: %s", key, exc)
    return states


def allocate_task(
    storage: StorageBackend,
    dataset_name: str,
    manifest: List[ManifestItem],
    session_id: str,
    speaker_gender: str,
    accent: Optional[str] = None,
    preferred_pair_id: Optional[str] = None,
    known_task_id: Optional[str] = None,
    max_retries: int = 15,
) -> Tuple[Optional[ManifestItem], Optional[TaskState], Optional[str]]:
    now = datetime.now(timezone.utc)
    manifest_by_id = {item.id: item for item in manifest}

    if known_task_id and known_task_id in manifest_by_id:
        st, etag = get_task_state(storage, dataset_name, known_task_id)
        if st and st.status == TaskStatus.RESERVED and st.recording_lease:
            if st.recording_lease.session_id == session_id and not st.recording_lease.is_expired(now):
                return manifest_by_id[known_task_id], st, etag

    existing_states = load_all_task_states(storage, dataset_name)

    for task_id, (st, etag) in existing_states.items():
        if (
            st.status == TaskStatus.RESERVED
            and st.recording_lease
            and st.recording_lease.session_id == session_id
            and not st.recording_lease.is_expired(now)
            and task_id in manifest_by_id
        ):
            return manifest_by_id[task_id], st, etag

    eligible_items = [item for item in manifest if item.is_eligible(speaker_gender)]

    candidate_items: List[ManifestItem] = []
    for item in eligible_items:
        if item.id in existing_states:
            st, _ = existing_states[item.id]
            if st.status == TaskStatus.RECORDED:
                continue
            if st.status == TaskStatus.RESERVED and st.recording_lease and not st.recording_lease.is_expired(now):
                continue
            candidate_items.append(item)
        else:
            candidate_items.append(item)

    if not candidate_items:
        return None, None, None

    def priority_score(item: ManifestItem) -> Tuple[int, int]:
        pair_match = 1 if (preferred_pair_id and item.pair_id == preferred_pair_id) else 0
        gender_match = 1 if item.planned_voice_gender.value == speaker_gender.lower() else 0
        return (pair_match, gender_match)

    ordered_candidates = sorted(
        candidate_items,
        key=lambda item: (
            priority_score(item),
            hashlib.sha256(f"{session_id}:{item.id}".encode()).digest(),
        ),
        reverse=True,
    )

    expires_dt = now + timedelta(seconds=DEFAULT_LEASE_TTL_SECONDS)
    lease_id = str(uuid.uuid4())

    new_lease = RecordingLease(
        lease_id=lease_id,
        session_id=session_id,
        speaker_gender=speaker_gender,
        accent=accent,
        created_at=now.isoformat(),
        expires_at=expires_dt.isoformat(),
    )

    for item in ordered_candidates[:max_retries]:
        key = _state_key(dataset_name, item.id)
        st, etag = get_task_state(storage, dataset_name, item.id)

        if st is None:
            new_state = TaskState(
                task_id=item.id,
                status=TaskStatus.RESERVED,
                recording_lease=new_lease,
                updated_at=utc_now_iso(),
            )
            state_bytes = json.dumps(new_state.model_dump(), ensure_ascii=False, indent=2).encode("utf-8")
            try:
                new_etag = storage.put_object_if_none_match(
                    key, state_bytes, content_type="application/json"
                )
                return item, new_state, new_etag
            except StoragePreconditionFailedError:
                winner, winner_etag = get_task_state(storage, dataset_name, item.id)
                if _owns_lease(winner, session_id):
                    return item, winner, winner_etag
                continue
        else:
            if st.status == TaskStatus.RECORDED:
                continue
            if st.status == TaskStatus.RESERVED and st.recording_lease and not st.recording_lease.is_expired(now):
                if _owns_lease(st, session_id):
                    return item, st, etag
                continue

            st.status = TaskStatus.RESERVED
            st.recording_lease = new_lease
            st.updated_at = utc_now_iso()
            state_bytes = json.dumps(st.model_dump(), ensure_ascii=False, indent=2).encode("utf-8")
            try:
                new_etag = storage.put_object_if_match(
                    key, state_bytes, etag, content_type="application/json"
                )
                return item, st, new_etag
            except StoragePreconditionFailedError:
                winner, winner_etag = get_task_state(storage, dataset_name, item.id)
                if _owns_lease(winner, session_id):
                    return item, winner, winner_etag
                continue

    return None, None, None


def _owns_lease(state: Optional[TaskState], session_id: str) -> bool:
    return bool(
        state
        and state.status == TaskStatus.RESERVED
        and state.recording_lease
        and state.recording_lease.session_id == session_id
        and not state.recording_lease.is_expired()
    )


def renew_lease(
    storage: StorageBackend,
    dataset_name: str,
    task_id: str,
    lease_id: str,
    ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
) -> Tuple[bool, str, Optional[TaskState]]:
    st, etag = get_task_state(storage, dataset_name, task_id)
    if not st or st.status != TaskStatus.RESERVED or not st.recording_lease:
        return False, "Không tìm thấy phiên giữ câu hợp lệ.", None

    if st.recording_lease.lease_id != lease_id:
        return False, "Mã giữ câu không khớp.", None

    now = datetime.now(timezone.utc)
    if st.recording_lease.is_expired(now):
        return False, "Phiên giữ câu đã hết hạn.", None

    expires_dt = now + timedelta(seconds=ttl_seconds)
    st.recording_lease.expires_at = expires_dt.isoformat()
    st.updated_at = utc_now_iso()

    key = _state_key(dataset_name, task_id)
    state_bytes = json.dumps(st.model_dump(), ensure_ascii=False, indent=2).encode("utf-8")
    try:
        storage.put_object_if_match(key, state_bytes, etag, content_type="application/json")
        return True, "Gia hạn thành công.", st
    except StoragePreconditionFailedError:
        return False, "Xung đột khi gia hạn phiên giữ câu.", None


def release_lease(
    storage: StorageBackend,
    dataset_name: str,
    task_id: str,
    lease_id: str,
) -> bool:
    st, etag = get_task_state(storage, dataset_name, task_id)
    if not st or st.status != TaskStatus.RESERVED or not st.recording_lease:
        return True

    if st.recording_lease.lease_id != lease_id:
        return False

    st.status = TaskStatus.AVAILABLE
    st.recording_lease = None
    st.updated_at = utc_now_iso()

    key = _state_key(dataset_name, task_id)
    state_bytes = json.dumps(st.model_dump(), ensure_ascii=False, indent=2).encode("utf-8")
    try:
        storage.put_object_if_match(key, state_bytes, etag, content_type="application/json")
        return True
    except StoragePreconditionFailedError:
        return False


def submit_recording(
    storage: StorageBackend,
    dataset_name: str,
    task_id: str,
    lease_id: str,
    session_id: str,
    audio_bytes: bytes,
    speaker_gender: str,
    accent: Optional[str] = None,
    idempotency_token: Optional[str] = None,
) -> Tuple[bool, str, Optional[TaskState]]:
    try:
        valid_wav, duration, rate, channels, sha256 = validate_and_process_audio(audio_bytes)
    except AudioValidationError as exc:
        return False, f"Âm thanh không hợp lệ: {exc}", None

    st, etag = get_task_state(storage, dataset_name, task_id)
    if not st:
        return False, "Không tìm thấy trạng thái của câu này.", None

    if st.status == TaskStatus.RECORDED and st.current_audio:
        if (
            st.current_audio.speaker_id == session_id
            and st.current_audio.sha256 == sha256
            and (not idempotency_token or st.current_audio.recording_id == idempotency_token)
        ):
            return True, "Bản ghi đã được gửi thành công.", st
        return False, "Câu này đã được ghi nhận bản ghi khác.", None

    if st.status != TaskStatus.RESERVED or not st.recording_lease:
        return False, "Câu chưa được giữ hoặc đã bị giải phóng.", None

    if st.recording_lease.lease_id != lease_id or st.recording_lease.session_id != session_id:
        return False, "Mã phiên giữ câu không trùng khớp.", None

    now = datetime.now(timezone.utc)
    if st.recording_lease.is_expired(now):
        return False, "Thời hạn giữ câu đã hết. Bản ghi chưa thể lưu.", None

    recording_uuid = idempotency_token or str(uuid.uuid4())
    audio_key = f"{dataset_name}/audio/{task_id}/{recording_uuid}.wav"

    try:
        storage.put_object_if_none_match(audio_key, valid_wav, content_type="audio/wav")
    except StoragePreconditionFailedError:
        saved, _ = storage.get_object(audio_key)
        if hashlib.sha256(saved).hexdigest() != sha256:
            return False, "Nội dung bản ghi không khớp lần gửi trước. Vui lòng thu lại.", None

    st, etag = get_task_state(storage, dataset_name, task_id)
    if not _owns_lease(st, session_id) or st.recording_lease.lease_id != lease_id:
        return False, "Câu này không còn được giữ cho bạn hoặc đã hết hạn.", None
    if st.recording_lease.is_expired(datetime.now(timezone.utc)):
        return False, "Thời hạn giữ câu đã hết. Bản ghi chưa thể lưu.", None

    old_audio_key = st.current_audio.object_key if st.current_audio else None

    audio_meta = AudioMetadata(
        recording_id=recording_uuid,
        object_key=audio_key,
        speaker_id=session_id,
        voice_gender=speaker_gender,
        accent=accent,
        sha256=sha256,
        duration_seconds=round(duration, 2),
        sample_rate=rate,
        channels=channels,
        uploaded_at=utc_now_iso(),
    )

    st.status = TaskStatus.RECORDED
    st.recording_lease = None
    st.current_audio = audio_meta
    st.review = ReviewRecord(status=ReviewStatus.UNCHECKED)
    st.updated_at = utc_now_iso()

    key = _state_key(dataset_name, task_id)
    state_bytes = json.dumps(st.model_dump(), ensure_ascii=False, indent=2).encode("utf-8")

    try:
        storage.put_object_if_match(key, state_bytes, etag, content_type="application/json")
        if old_audio_key and old_audio_key != audio_key:
            try:
                storage.delete_object(old_audio_key)
            except Exception:
                pass
        return True, "Gửi bản ghi thành công!", st
    except StoragePreconditionFailedError:
        logger.error("CAS conflict when committing audio for %s.", task_id)
        return False, "Xung đột trạng thái khi lưu bản ghi. Vui lòng thử lại.", None


def get_dataset_summary(
    storage: StorageBackend,
    dataset_name: str,
    manifest: List[ManifestItem],
) -> Dict[str, Any]:
    total_tasks = len(manifest)
    existing_states = load_all_task_states(storage, dataset_name)
    now = datetime.now(timezone.utc)

    recorded_count = 0
    reserved_count = 0
    unchecked_count = 0
    approved_count = 0
    approved_edited_count = 0
    rejected_count = 0
    recorded_males = 0
    recorded_females = 0

    scenarios_recorded: Dict[str, int] = {}
    manifest_by_id = {item.id: item for item in manifest}

    for task_id, (st, _) in existing_states.items():
        if st.status == TaskStatus.RECORDED:
            recorded_count += 1
            if st.current_audio:
                if st.current_audio.voice_gender.lower() == "male":
                    recorded_males += 1
                elif st.current_audio.voice_gender.lower() == "female":
                    recorded_females += 1

            if task_id in manifest_by_id:
                sc = manifest_by_id[task_id].scenario
                scenarios_recorded[sc] = scenarios_recorded.get(sc, 0) + 1

            r_status = st.review.status if st.review else ReviewStatus.UNCHECKED
            if r_status == ReviewStatus.UNCHECKED:
                unchecked_count += 1
            elif r_status == ReviewStatus.APPROVED:
                approved_count += 1
            elif r_status == ReviewStatus.APPROVED_EDITED:
                approved_edited_count += 1
            elif r_status == ReviewStatus.REJECTED:
                rejected_count += 1

        elif st.status == TaskStatus.RESERVED and st.recording_lease and not st.recording_lease.is_expired(now):
            reserved_count += 1

    available_count = max(0, total_tasks - recorded_count - reserved_count)

    return {
        "total_tasks": total_tasks,
        "recorded": recorded_count,
        "available": available_count,
        "reserved": reserved_count,
        "unrecorded": available_count + reserved_count,
        "unchecked": unchecked_count,
        "approved": approved_count,
        "approved_edited": approved_edited_count,
        "reviewed_total": approved_count + approved_edited_count,
        "recorded_males": recorded_males,
        "recorded_females": recorded_females,
        "scenarios_recorded": scenarios_recorded,
    }
