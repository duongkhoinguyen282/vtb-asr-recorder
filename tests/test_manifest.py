"""Tests for manifest validation, schema compliance, and fixture facts."""

import json
import os
import pytest

from schemas import VoiceEligibility, VoiceGender
from services.manifest import validate_manifest_content


def test_initial_manifest_fixture_facts():
    """Acceptance Criteria 1: Verify authoritative facts of the initial manifest.jsonl fixture.

    Facts:
    - Exactly 500 unique tasks
    - Planned voices: 250 male / 250 female
    - 30 matched-pair groups, each containing 2 scripts
    - No conflicting eligibility vs planned voice gender
    """
    manifest_path = "manifest.jsonl"
    assert os.path.exists(manifest_path), "manifest.jsonl must exist in workspace root"

    with open(manifest_path, "rb") as f:
        content = f.read()

    items, errors = validate_manifest_content(content)
    assert len(errors) == 0, f"Initial manifest should have 0 validation errors: {errors[:5]}"
    assert len(items) == 500, f"Expected exactly 500 items, got {len(items)}"

    # Check uniqueness of IDs
    unique_ids = {item.id for item in items}
    assert len(unique_ids) == 500

    # Check planned voice distribution
    planned_males = sum(1 for it in items if it.planned_voice_gender == VoiceGender.MALE)
    planned_females = sum(1 for it in items if it.planned_voice_gender == VoiceGender.FEMALE)
    assert planned_males == 250, f"Expected 250 planned male, got {planned_males}"
    assert planned_females == 250, f"Expected 250 planned female, got {planned_females}"

    # Check eligibility routing: no conflict with planned gender
    for it in items:
        if it.speaker_voice_eligibility == VoiceEligibility.MALE:
            assert it.planned_voice_gender == VoiceGender.MALE
        elif it.speaker_voice_eligibility == VoiceEligibility.FEMALE:
            assert it.planned_voice_gender == VoiceGender.FEMALE

    # Check matched pairs
    pair_counts = {}
    for it in items:
        if it.pair_id:
            pair_counts[it.pair_id] = pair_counts.get(it.pair_id, 0) + 1

    assert len(pair_counts) == 30, f"Expected exactly 30 matched pairs, got {len(pair_counts)}"
    for pid, count in pair_counts.items():
        assert count == 2, f"Pair {pid} must have exactly 2 scripts, got {count}"


def test_manifest_validation_rejects_malformed_json():
    """Verify that malformed JSON lines are caught with line numbers."""
    bad_content = '{"id": "T1", "reading_script": "Một hai"}\nTHIS IS NOT JSON\n'
    items, errors = validate_manifest_content(bad_content)
    assert len(errors) > 0
    assert any("Dòng 2" in err for err in errors)


def test_manifest_validation_rejects_missing_required_fields():
    """Verify missing required fields trigger descriptive errors."""
    bad_record = {
        "id": "T-01",
        "reading_script": "Không một hai",
        # missing delivery_instruction, domain, scenario, etc.
    }
    items, errors = validate_manifest_content(json.dumps(bad_record))
    assert len(errors) > 0
    assert any("Thiếu hoặc rỗng các trường bắt buộc" in err for err in errors)


def test_manifest_validation_rejects_duplicate_ids():
    """Verify duplicate task IDs are rejected atomically."""
    row1 = {
        "id": "DUP-01",
        "reading_script": "Câu 1",
        "delivery_instruction": "Đọc rõ",
        "domain": "phone",
        "scenario": "restart",
        "speaker_voice_eligibility": "any",
        "planned_voice_gender": "male",
    }
    row2 = dict(row1)
    row2["reading_script"] = "Câu 2 cùng ID"

    content = f"{json.dumps(row1)}\n{json.dumps(row2)}"
    items, errors = validate_manifest_content(content)
    assert len(errors) > 0
    assert any("Trùng lặp mã task 'id' 'DUP-01'" in err for err in errors)


def test_manifest_validation_rejects_invalid_enums_and_conflicts():
    """Verify invalid gender enums and conflicting eligibility vs planned gender are rejected."""
    # Invalid enum
    bad_enum = {
        "id": "T-ENUM",
        "reading_script": "Câu thử",
        "delivery_instruction": "Đọc nhanh",
        "domain": "phone",
        "scenario": "restart",
        "speaker_voice_eligibility": "robot",  # invalid
        "planned_voice_gender": "male",
    }
    _, errors1 = validate_manifest_content(json.dumps(bad_enum))
    assert any("speaker_voice_eligibility" in err for err in errors1)

    # Conflicting routing: eligibility=male but planned=female
    conflict_row = {
        "id": "T-CONFLICT",
        "reading_script": "Câu thử",
        "delivery_instruction": "Đọc nhanh",
        "domain": "phone",
        "scenario": "restart",
        "speaker_voice_eligibility": "male",
        "planned_voice_gender": "female",  # conflict!
    }
    _, errors2 = validate_manifest_content(json.dumps(conflict_row))
    assert any("Xung đột giới tính" in err for err in errors2)


def test_manifest_validation_rejects_incomplete_pairs():
    """Verify unmatched pair group (< 2 items) is caught."""
    single_pair_row = {
        "id": "PAIR-SOLO",
        "pair_id": "PAIR-999",
        "reading_script": "Câu lẻ cặp",
        "delivery_instruction": "Đọc bình thường",
        "domain": "phone",
        "scenario": "hesitation",
        "speaker_voice_eligibility": "any",
        "planned_voice_gender": "female",
    }
    _, errors = validate_manifest_content(json.dumps(single_pair_row))
    assert any("Chỉ có 1 câu" in err for err in errors)

