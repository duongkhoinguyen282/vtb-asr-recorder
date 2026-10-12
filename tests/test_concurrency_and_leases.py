"""Tests for concurrent task allocation, leases, expiration, and submission."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import pytest

from schemas import ManifestItem, TaskStatus, VoiceEligibility, VoiceGender
from services.audio import validate_and_process_audio
from services.storage import MockStorage
from services.tasks import (
    allocate_task,
    get_task_state,
    release_lease,
    renew_lease,
    submit_recording,
)
from tests.test_audio import make_dummy_wav


def make_test_manifest(count: int = 5) -> list[ManifestItem]:
    items = []
    for i in range(1, count + 1):
        items.append(
            ManifestItem(
                id=f"TEST-{i:04d}",
                reading_script=f"Kịch bản câu số {i}",
                delivery_instruction="Đọc rõ ràng",
                domain="cccd_12",
                scenario="restart",
                speaker_voice_eligibility=VoiceEligibility.ANY,
                planned_voice_gender=VoiceGender.MALE if i % 2 == 1 else VoiceGender.FEMALE,
            )
        )
    return items


def test_concurrent_readers_no_duplicate_leases():
    """Acceptance Criteria 2: 20 concurrent readers competing for 5 tasks.

    Must result in at most 5 leased tasks, with NO two readers holding active lease on the same task.
    """
    storage = MockStorage()
    manifest = make_test_manifest(count=5)
    dataset_name = "test-dataset"

    claimed_tasks = []

    def claim_worker(reader_idx: int):
        session_id = f"reader-session-{reader_idx}"
        item, state, etag = allocate_task(
            storage=storage,
            dataset_name=dataset_name,
            manifest=manifest,
            session_id=session_id,
            speaker_gender="male",
        )
        if item and state:
            return (reader_idx, item.id, state.recording_lease.lease_id)
        return None

    with ThreadPoolExecutor(max_workers=20) as executor:
        futures = [executor.submit(claim_worker, i) for i in range(20)]
        results = [f.result() for f in futures if f.result() is not None]

    assert len(results) <= 5, "Cannot lease more tasks than exist in the pool"
    leased_task_ids = [r[1] for r in results]
    # Verify no duplicate leases on same task
    assert len(leased_task_ids) == len(set(leased_task_ids)), "Duplicate active leases detected!"


def test_expired_lease_can_be_reclaimed():
    """Verify an expired lease is safely reclaimed by another reader."""
    storage = MockStorage()
    manifest = make_test_manifest(count=1)
    dataset_name = "test-dataset"

    # Reader 1 claims the single task
    item1, state1, _ = allocate_task(
        storage, dataset_name, manifest, session_id="s1", speaker_gender="female"
    )
    assert item1 is not None and state1 is not None

    # Artificially expire the lease
    past_time = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
    state1.recording_lease.expires_at = past_time
    storage.put_object(f"{dataset_name}/states/{item1.id}.json", state1.model_dump_json().encode("utf-8"))

    # Reader 2 should now be able to claim it
    item2, state2, _ = allocate_task(
        storage, dataset_name, manifest, session_id="s2", speaker_gender="female"
    )
    assert item2 is not None
    assert item2.id == item1.id
    assert state2.recording_lease.session_id == "s2"


def test_release_lease_allows_immediate_reallocation():
    """Verify releasing lease ('Bỏ qua') immediately allows another reader to claim."""
    storage = MockStorage()
    manifest = make_test_manifest(count=1)
    dataset_name = "test-dataset"

    item, state, _ = allocate_task(
        storage, dataset_name, manifest, session_id="s1", speaker_gender="male"
    )
    assert item is not None

    # Reader 1 releases lease
    released = release_lease(storage, dataset_name, item.id, state.recording_lease.lease_id)
    assert released is True

    # Reader 2 claims the now-available task
    item2, state2, _ = allocate_task(
        storage, dataset_name, manifest, session_id="s2", speaker_gender="male"
    )
    assert item2 is not None
    assert state2.recording_lease.session_id == "s2"


def test_stale_lease_cannot_submit():
    """Verify stale submit after lease expiry/reclaim fails and does not overwrite."""
    storage = MockStorage()
    manifest = make_test_manifest(count=1)
    dataset_name = "test-dataset"

    item, state1, _ = allocate_task(
        storage, dataset_name, manifest, session_id="s1", speaker_gender="male"
    )
    lease_id_1 = state1.recording_lease.lease_id

    # Expire lease 1 and let Reader 2 reclaim it
    past_time = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
    state1.recording_lease.expires_at = past_time
    storage.put_object(f"{dataset_name}/states/{item.id}.json", state1.model_dump_json().encode("utf-8"))

    item2, state2, _ = allocate_task(
        storage, dataset_name, manifest, session_id="s2", speaker_gender="male"
    )
    assert state2.recording_lease.session_id == "s2"

    # Reader 1 attempts late submit with stale lease
    audio_bytes = make_dummy_wav(duration=1.0)
    ok, msg, _ = submit_recording(
        storage=storage,
        dataset_name=dataset_name,
        task_id=item.id,
        lease_id=lease_id_1,
        session_id="s1",
        audio_bytes=audio_bytes,
        speaker_gender="male",
    )
    assert ok is False
    assert "hết hạn" in msg or "không trùng khớp" in msg

    # Task should still be held by Reader 2
    fresh_state, _ = get_task_state(storage, dataset_name, item.id)
    assert fresh_state.status == TaskStatus.RESERVED
    assert fresh_state.recording_lease.session_id == "s2"


def test_successful_submit_and_idempotency():
    """Verify upload transitions to RECORDED, and double-submit is idempotent."""
    storage = MockStorage()
    manifest = make_test_manifest(count=1)
    dataset_name = "test-dataset"

    item, state, _ = allocate_task(
        storage, dataset_name, manifest, session_id="s1", speaker_gender="female"
    )
    audio_bytes = make_dummy_wav(duration=2.0)

    # First submit
    ok1, msg1, st1 = submit_recording(
        storage=storage,
        dataset_name=dataset_name,
        task_id=item.id,
        lease_id=state.recording_lease.lease_id,
        session_id="s1",
        audio_bytes=audio_bytes,
        speaker_gender="female",
    )
    assert ok1 is True
    assert st1.status == TaskStatus.RECORDED
    assert st1.current_audio is not None

    # Second submit (double click or retry)
    ok2, msg2, st2 = submit_recording(
        storage=storage,
        dataset_name=dataset_name,
        task_id=item.id,
        lease_id=state.recording_lease.lease_id,
        session_id="s1",
        audio_bytes=audio_bytes,
        speaker_gender="female",
    )
    assert ok2 is True
    assert st2.status == TaskStatus.RECORDED
    assert st2.current_audio.recording_id == st1.current_audio.recording_id

