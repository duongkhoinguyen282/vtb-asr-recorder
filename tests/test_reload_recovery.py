"""Tests for reload recovery, session tokens, and multi-tab policies."""

from datetime import datetime, timedelta, timezone
from schemas import TaskStatus
from services.storage import MockStorage
from services.tasks import allocate_task, get_task_state
from tests.test_concurrency_and_leases import make_test_manifest


def test_reload_recovers_same_active_task():
    """Acceptance Criteria 4: When a reader refreshes (F5), the active lease and task are restored."""
    storage = MockStorage()
    manifest = make_test_manifest(count=3)
    dataset_name = "test-dataset"
    session_id = "reader-opaque-session-token"

    # Initial claim
    item1, state1, _ = allocate_task(
        storage=storage,
        dataset_name=dataset_name,
        manifest=manifest,
        session_id=session_id,
        speaker_gender="male",
    )
    assert item1 is not None

    # Browser refresh: allocate_task with known_task_id or same session_id
    item2, state2, _ = allocate_task(
        storage=storage,
        dataset_name=dataset_name,
        manifest=manifest,
        session_id=session_id,
        speaker_gender="male",
        known_task_id=item1.id,
    )

    assert item2.id == item1.id, "Refresh must return the same task"
    assert state2.recording_lease.lease_id == state1.recording_lease.lease_id


def test_reload_after_lease_expiry_recovers_gracefully():
    """Verify reload after lease has expired allocates a new task rather than crashing."""
    storage = MockStorage()
    manifest = make_test_manifest(count=3)
    dataset_name = "test-dataset"
    session_id = "reader-session"

    item1, state1, _ = allocate_task(
        storage=storage,
        dataset_name=dataset_name,
        manifest=manifest,
        session_id=session_id,
        speaker_gender="male",
    )

    # Expire the lease
    past_time = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    state1.recording_lease.expires_at = past_time
    storage.put_object(f"{dataset_name}/states/{item1.id}.json", state1.model_dump_json().encode("utf-8"))

    # Reload: known_task_id lease is expired
    item2, state2, _ = allocate_task(
        storage=storage,
        dataset_name=dataset_name,
        manifest=manifest,
        session_id=session_id,
        speaker_gender="male",
        known_task_id=item1.id,
    )

    assert item2 is not None
    # Can reclaim item1 or another available task with a fresh unexpired lease
    assert not state2.recording_lease.is_expired()


def test_multi_tab_same_session_reuses_single_lease():
    """Verify opening two tabs with the same session ID reuses the existing lease."""
    storage = MockStorage()
    manifest = make_test_manifest(count=5)
    dataset_name = "test-dataset"
    session_id = "shared-reader-session"

    # Tab 1 claims task
    tab1_item, tab1_state, _ = allocate_task(
        storage=storage,
        dataset_name=dataset_name,
        manifest=manifest,
        session_id=session_id,
        speaker_gender="female",
    )

    # Tab 2 opens with same session_id (e.g. copied URL or synced session)
    tab2_item, tab2_state, _ = allocate_task(
        storage=storage,
        dataset_name=dataset_name,
        manifest=manifest,
        session_id=session_id,
        speaker_gender="female",
    )

    assert tab2_item.id == tab1_item.id, "Second tab must reuse the active lease, not claim a new task"
    assert tab2_state.recording_lease.lease_id == tab1_state.recording_lease.lease_id

