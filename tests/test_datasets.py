"""Tests for dataset isolation, [+] add dataset, activation, and count metrics."""

import json
import pytest

from schemas import TaskStatus
from services.dataset_registry import (
    create_dataset,
    ensure_initialized,
    get_registry,
    set_active_dataset,
)
from services.manifest import load_manifest_from_storage
from services.review import approve_recording, claim_review_lease, reject_recording
from services.storage import MockStorage
from services.tasks import (
    allocate_task,
    get_dataset_summary,
    get_task_state,
    submit_recording,
)
from tests.test_audio import make_dummy_wav


def test_add_dataset_isolation_and_explicit_activation():
    """Acceptance Criteria 7: Creating second dataset changes neither original manifest nor active dataset until explicit activation."""
    storage = MockStorage()

    # 1. Bootstrap numeric-data-v1 from manifest.jsonl
    reg = ensure_initialized(storage, bootstrap_manifest_path="manifest.jsonl")
    assert reg.active_dataset == "numeric-data-v1"
    manifest_v1 = load_manifest_from_storage(storage, "numeric-data-v1")
    assert len(manifest_v1) == 500

    # 2. Add second dataset via create_dataset
    row_v2 = {
        "id": "V2-0001",
        "reading_script": "Tập hai câu một",
        "delivery_instruction": "Đọc chậm",
        "domain": "test",
        "scenario": "restart",
        "speaker_voice_eligibility": "any",
        "planned_voice_gender": "male",
    }
    manifest_bytes_v2 = json.dumps(row_v2).encode("utf-8")

    ok, msg, errors = create_dataset(
        storage=storage,
        name="numeric-data-v2",
        display_name="Tập dữ liệu đợt 2",
        manifest_bytes=manifest_bytes_v2,
    )
    assert ok is True, f"Failed to create dataset: {msg}"

    # 3. Verify active dataset is STILL numeric-data-v1!
    reg_after, _ = get_registry(storage)
    assert reg_after.active_dataset == "numeric-data-v1"
    assert "numeric-data-v2" in reg_after.datasets

    # Original manifest v1 remains completely unchanged
    manifest_v1_check = load_manifest_from_storage(storage, "numeric-data-v1")
    assert len(manifest_v1_check) == 500

    # Reader querying active dataset still gets numeric-data-v1
    reader_item, _, _ = allocate_task(
        storage, reg_after.active_dataset, manifest_v1_check, session_id="s1", speaker_gender="male"
    )
    assert reader_item.id.startswith("HNUM-")

    # 4. Explicit activation of numeric-data-v2
    activated = set_active_dataset(storage, "numeric-data-v2")
    assert activated is True

    reg_active, _ = get_registry(storage)
    assert reg_active.active_dataset == "numeric-data-v2"

    manifest_v2 = load_manifest_from_storage(storage, "numeric-data-v2")
    assert len(manifest_v2) == 1
    assert manifest_v2[0].id == "V2-0001"

    # Tasks recorded in v2 are isolated from v1
    v2_item, v2_state, _ = allocate_task(
        storage, "numeric-data-v2", manifest_v2, session_id="s2", speaker_gender="male"
    )
    assert v2_item.id == "V2-0001"
    v1_check_state, _ = get_task_state(storage, "numeric-data-v1", "V2-0001")
    assert v1_check_state is None, "State must not leak into numeric-data-v1 namespace"


def test_create_dataset_atomic_rollback_on_failure():
    """Verify failed dataset creation leaves no partial keys or corrupted registry."""
    storage = MockStorage()
    ensure_initialized(storage, bootstrap_manifest_path="manifest.jsonl")

    # Invalid manifest content (missing required fields)
    bad_manifest = b'{"id": "BAD-1", "reading_script": "only script"}\n'
    ok, msg, errors = create_dataset(
        storage=storage,
        name="bad-dataset",
        display_name="Bad Dataset",
        manifest_bytes=bad_manifest,
    )
    assert ok is False
    assert len(errors) > 0

    # Verify no partial keys created
    assert storage.head_object("bad-dataset/manifest.jsonl") is None
    assert storage.head_object("bad-dataset/config.json") is None

    reg, _ = get_registry(storage)
    assert "bad-dataset" not in reg.datasets


def test_recorded_count_metrics_lifecycle():
    """Acceptance Criteria 8: Recorded count increases immediately on upload (unchecked); approving doesn't change it; rejecting does; re-recording increases it."""
    storage = MockStorage()
    ensure_initialized(storage, bootstrap_manifest_path="manifest.jsonl")
    manifest = load_manifest_from_storage(storage, "numeric-data-v1")
    ds = "numeric-data-v1"

    # Initial: 0 recorded
    s0 = get_dataset_summary(storage, ds, manifest)
    assert s0["recorded"] == 0

    # 1. Allocate and submit
    item, st, _ = allocate_task(storage, ds, manifest, "s1", "male")
    submit_recording(storage, ds, item.id, st.recording_lease.lease_id, "s1", make_dummy_wav(1.5), "male")

    # Recorded count INCREASES IMMEDIATELY (even though review is unchecked!)
    s1 = get_dataset_summary(storage, ds, manifest)
    assert s1["recorded"] == 1
    assert s1["unchecked"] == 1

    # 2. Approve recording -> recorded count DOES NOT change
    _, _, lease = claim_review_lease(storage, ds, item.id, "admin1")
    approve_recording(storage, ds, item.id, lease.lease_id, "admin1")

    s2 = get_dataset_summary(storage, ds, manifest)
    assert s2["recorded"] == 1
    assert s2["approved"] == 1
    assert s2["unchecked"] == 0

    # 3. Reject recording -> recorded count DECREASES immediately
    _, _, lease2 = claim_review_lease(storage, ds, item.id, "admin1")
    reject_recording(storage, ds, item.id, lease2.lease_id, "admin1", error_reasons=["Ồn"])

    s3 = get_dataset_summary(storage, ds, manifest)
    assert s3["recorded"] == 0
    assert s3["available"] == 500

    # 4. Re-recording -> recorded count increases again
    item_re, st_re, _ = allocate_task(storage, ds, manifest, "s2", "male")
    submit_recording(storage, ds, item_re.id, st_re.recording_lease.lease_id, "s2", make_dummy_wav(2.0), "male")

    s4 = get_dataset_summary(storage, ds, manifest)
    assert s4["recorded"] == 1
    assert s4["unchecked"] == 1

