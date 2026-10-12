"""Tests for admin authentication, review leases, QA decisions, and re-recording."""

from concurrent.futures import ThreadPoolExecutor
import streamlit as st

from schemas import ReviewStatus, TaskStatus
from services.review import (
    approve_recording,
    claim_review_lease,
    reject_recording,
)
from services.session import verify_admin_password
from services.storage import MockStorage
from services.tasks import (
    allocate_task,
    get_dataset_summary,
    get_task_state,
    submit_recording,
)
from tests.test_audio import make_dummy_wav
from tests.test_concurrency_and_leases import make_test_manifest


def test_admin_password_verification():
    """Acceptance Criteria 5: Constant-time admin password check and throttling."""
    correct_pw = "ilovevtb"

    # Reset test session state if needed
    if not hasattr(st, "session_state"):
        st.session_state = {}
    st.session_state.admin_failed_attempts = 0

    # Wrong password
    ok, msg = verify_admin_password("wrong_password", correct_pw)
    assert ok is False
    assert "Mật khẩu không đúng" in msg

    # Correct password
    ok, msg = verify_admin_password("ilovevtb", correct_pw)
    assert ok is True
    assert "thành công" in msg


def test_dashboard_does_not_claim_tasks():
    """Verify loading dashboard metrics does not claim tasks or alter state."""
    storage = MockStorage()
    manifest = make_test_manifest(count=5)
    dataset_name = "test-dataset"

    summary_before = get_dataset_summary(storage, dataset_name, manifest)
    assert summary_before["total_tasks"] == 5
    assert summary_before["recorded"] == 0
    assert summary_before["available"] == 5
    assert summary_before["reserved"] == 0

    # States must still be empty
    states = storage.list_objects(prefix=f"{dataset_name}/states/")
    assert len(states) == 0


def test_three_review_decisions_and_rerecord():
    """Acceptance Criteria 5 & 8: Verify Approve, Approve with Edit, Reject -> Available -> Re-record."""
    storage = MockStorage()
    manifest = make_test_manifest(count=1)
    dataset_name = "test-dataset"

    # 1. Reader records task
    item, state, _ = allocate_task(storage, dataset_name, manifest, session_id="s1", speaker_gender="male")
    wav1 = make_dummy_wav(duration=1.5)
    submit_ok, _, state = submit_recording(
        storage=storage,
        dataset_name=dataset_name,
        task_id=item.id,
        lease_id=state.recording_lease.lease_id,
        session_id="s1",
        audio_bytes=wav1,
        speaker_gender="male",
    )
    assert submit_ok is True
    assert state.status == TaskStatus.RECORDED
    assert state.review.status == ReviewStatus.UNCHECKED

    summary1 = get_dataset_summary(storage, dataset_name, manifest)
    assert summary1["recorded"] == 1
    assert summary1["unchecked"] == 1

    # Decision 1: Approve
    l_ok, _, lease = claim_review_lease(storage, dataset_name, item.id, reviewer_id="admin1")
    assert l_ok is True
    app_ok, _ = approve_recording(
        storage, dataset_name, item.id, lease.lease_id, reviewer_id="admin1"
    )
    assert app_ok is True

    st_app, _ = get_task_state(storage, dataset_name, item.id)
    assert st_app.status == TaskStatus.RECORDED
    assert st_app.review.status == ReviewStatus.APPROVED

    # Decision 2: Re-review with Edit
    l_ok2, _, lease2 = claim_review_lease(storage, dataset_name, item.id, reviewer_id="admin1")
    assert l_ok2 is True
    edit_ok, _ = approve_recording(
        storage,
        dataset_name,
        item.id,
        lease2.lease_id,
        reviewer_id="admin1",
        verified_transcript="Một hai ba bốn năm sáu",
    )
    assert edit_ok is True

    st_edit, _ = get_task_state(storage, dataset_name, item.id)
    assert st_edit.review.status == ReviewStatus.APPROVED_EDITED
    assert st_edit.review.verified_transcript == "Một hai ba bốn năm sáu"
    # Original script must remain untouched!
    assert item.reading_script != "Một hai ba bốn năm sáu"

    # Decision 3: Reject for re-recording
    l_ok3, _, lease3 = claim_review_lease(storage, dataset_name, item.id, reviewer_id="admin2")
    assert l_ok3 is True
    rej_ok, _ = reject_recording(
        storage,
        dataset_name,
        item.id,
        lease3.lease_id,
        reviewer_id="admin2",
        error_reasons=["Ồn / khó nghe"],
        note="Nhiều tạp âm phía sau",
    )
    assert rej_ok is True

    # State must now be AVAILABLE and drop from Recorded count
    st_rej, _ = get_task_state(storage, dataset_name, item.id)
    assert st_rej.status == TaskStatus.AVAILABLE
    assert len(st_rej.rejection_history) == 1
    assert st_rej.rejection_history[0].error_reasons == ["Ồn / khó nghe"]

    summary_rej = get_dataset_summary(storage, dataset_name, manifest)
    assert summary_rej["recorded"] == 0
    assert summary_rej["available"] == 1

    # Re-recording: another volunteer claims and records
    item_re, state_re, _ = allocate_task(
        storage, dataset_name, manifest, session_id="s2", speaker_gender="male"
    )
    assert item_re.id == item.id
    wav2 = make_dummy_wav(duration=2.2)
    submit2_ok, _, st_new = submit_recording(
        storage=storage,
        dataset_name=dataset_name,
        task_id=item.id,
        lease_id=state_re.recording_lease.lease_id,
        session_id="s2",
        audio_bytes=wav2,
        speaker_gender="male",
    )
    assert submit2_ok is True
    assert st_new.status == TaskStatus.RECORDED
    # Review resets to unchecked
    assert st_new.review.status == ReviewStatus.UNCHECKED

    summary_re = get_dataset_summary(storage, dataset_name, manifest)
    assert summary_re["recorded"] == 1
    assert summary_re["unchecked"] == 1


def test_concurrent_reviewers_exclusive_lease():
    """Acceptance Criteria 6: Three simultaneous reviewers competing for same recording.

    Exactly ONE gets the exclusive review lease; others are blocked by CAS conflict.
    """
    storage = MockStorage()
    manifest = make_test_manifest(count=1)
    dataset_name = "test-dataset"

    item, state, _ = allocate_task(storage, dataset_name, manifest, session_id="s1", speaker_gender="male")
    submit_recording(
        storage, dataset_name, item.id, state.recording_lease.lease_id, "s1", make_dummy_wav(1.0), "male"
    )

    def reviewer_worker(reviewer_idx: int):
        ok, msg, lease = claim_review_lease(
            storage, dataset_name, item.id, reviewer_id=f"reviewer-{reviewer_idx}"
        )
        return (reviewer_idx, ok, lease)

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = [executor.submit(reviewer_worker, i) for i in range(3)]
        results = [f.result() for f in futures]

    successes = [r for r in results if r[1] is True]
    assert len(successes) == 1, f"Exactly 1 reviewer must obtain exclusive lease, got {len(successes)}"

