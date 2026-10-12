"""Manifest parsing, schema validation, and storage retrieval with caching."""

from __future__ import annotations

import json
import logging
import threading
from typing import Dict, List, Optional, Tuple

from schemas import ManifestItem, VoiceEligibility, VoiceGender
from services.storage import StorageBackend, StorageNotFoundError

logger = logging.getLogger(__name__)

_MANIFEST_CACHE: Dict[str, List[ManifestItem]] = {}
_CACHE_LOCK = threading.Lock()


def validate_manifest_content(content: str | bytes) -> Tuple[List[ManifestItem], List[str]]:
    """Parse and validate JSONL manifest content.

    Returns:
        (items, errors): A list of parsed ManifestItem instances and a list of actionable error strings.
        If errors is non-empty, the manifest should be rejected atomically.
    """
    if isinstance(content, bytes):
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            return [], [f"Lỗi mã hóa tệp: Nội dung phải là UTF-8 hợp lệ ({exc})"]
    else:
        text = content

    lines = text.splitlines()
    if not lines or not any(line.strip() for line in lines):
        return [], ["Tệp manifest trống hoặc không có dòng dữ liệu hợp lệ."]

    items: List[ManifestItem] = []
    errors: List[str] = []
    seen_ids: Dict[str, int] = {}
    pair_groups: Dict[str, List[Tuple[int, str]]] = {}

    for line_idx, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line:
            continue

        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            errors.append(f"Dòng {line_idx}: JSON không hợp lệ - {exc.msg} (cột {exc.colno})")
            continue

        if not isinstance(record, dict):
            errors.append(f"Dòng {line_idx}: Mỗi dòng phải là một JSON object.")
            continue

        task_id = record.get("id")
        if not task_id or not str(task_id).strip():
            errors.append(f"Dòng {line_idx}: Thiếu trường 'id' hoặc id rỗng.")
            continue
        task_id_str = str(task_id).strip()

        # Check duplicate IDs
        if task_id_str in seen_ids:
            errors.append(
                f"Dòng {line_idx}: Trùng lặp mã task 'id' '{task_id_str}' (đã xuất hiện tại dòng {seen_ids[task_id_str]})."
            )
        else:
            seen_ids[task_id_str] = line_idx

        # Check required fields
        required_fields = [
            "reading_script",
            "delivery_instruction",
            "domain",
            "scenario",
            "speaker_voice_eligibility",
            "planned_voice_gender",
        ]
        missing_fields = [f for f in required_fields if f not in record or not str(record[f]).strip()]
        if missing_fields:
            errors.append(
                f"Dòng {line_idx} (Task ID '{task_id_str}'): Thiếu hoặc rỗng các trường bắt buộc: {', '.join(missing_fields)}"
            )
            continue

        eligibility_val = str(record.get("speaker_voice_eligibility", "")).strip().lower()
        if eligibility_val not in (VoiceEligibility.MALE.value, VoiceEligibility.FEMALE.value, VoiceEligibility.ANY.value):
            errors.append(
                f"Dòng {line_idx} (Task ID '{task_id_str}'): 'speaker_voice_eligibility' phải là 'male', 'female', hoặc 'any'. Nhận: '{eligibility_val}'"
            )

        planned_val = str(record.get("planned_voice_gender", "")).strip().lower()
        if planned_val not in (VoiceGender.MALE.value, VoiceGender.FEMALE.value):
            errors.append(
                f"Dòng {line_idx} (Task ID '{task_id_str}'): 'planned_voice_gender' phải là 'male' hoặc 'female'. Nhận: '{planned_val}'"
            )

        # Conflict check: eligibility vs planned
        if eligibility_val == VoiceEligibility.MALE.value and planned_val == VoiceGender.FEMALE.value:
            errors.append(
                f"Dòng {line_idx} (Task ID '{task_id_str}'): Xung đột giới tính - eligibility='male' nhưng planned_voice_gender='female'."
            )
        elif eligibility_val == VoiceEligibility.FEMALE.value and planned_val == VoiceGender.MALE.value:
            errors.append(
                f"Dòng {line_idx} (Task ID '{task_id_str}'): Xung đột giới tính - eligibility='female' nhưng planned_voice_gender='male'."
            )

        pair_id = record.get("pair_id")
        if pair_id:
            pair_id_str = str(pair_id).strip()
            if pair_id_str:
                pair_groups.setdefault(pair_id_str, []).append((line_idx, task_id_str))

        try:
            item = ManifestItem(**record)
            items.append(item)
        except Exception as exc:
            errors.append(f"Dòng {line_idx} (Task ID '{task_id_str}'): Lỗi xác thực dữ liệu - {exc}")

    # Validate pair consistency
    for pid, tasks in pair_groups.items():
        if len(tasks) < 2:
            errors.append(
                f"Nhóm cặp đối chứng '{pid}': Chỉ có {len(tasks)} câu ({tasks[0][1]} tại dòng {tasks[0][0]}). Cặp phải có ít nhất 2 câu đối chứng."
            )

    return items, errors


def load_manifest_from_storage(storage: StorageBackend, dataset_name: str) -> List[ManifestItem]:
    """Retrieve and cache the manifest for a dataset from storage."""
    with _CACHE_LOCK:
        if dataset_name in _MANIFEST_CACHE:
            return _MANIFEST_CACHE[dataset_name]

    key = f"{dataset_name}/manifest.jsonl"
    try:
        data, _ = storage.get_object(key)
    except StorageNotFoundError as exc:
        raise StorageNotFoundError(f"Không tìm thấy manifest tại '{key}'") from exc

    items, errors = validate_manifest_content(data)
    if errors:
        raise ValueError(
            f"Manifest lưu trữ tại '{key}' không hợp lệ: " + "; ".join(errors[:5])
        )

    with _CACHE_LOCK:
        _MANIFEST_CACHE[dataset_name] = items

    return items


def invalidate_manifest_cache(dataset_name: Optional[str] = None) -> None:
    """Invalidate cached manifest items."""
    with _CACHE_LOCK:
        if dataset_name:
            _MANIFEST_CACHE.pop(dataset_name, None)
        else:
            _MANIFEST_CACHE.clear()

