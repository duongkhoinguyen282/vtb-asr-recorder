"""Dataset registry management and bootstrap initialization."""

from __future__ import annotations

import json
import logging
import os
import re
from typing import List, Optional, Tuple

from schemas import DatasetConfig, DatasetsRegistry, utc_now_iso
from services.manifest import invalidate_manifest_cache, validate_manifest_content
from services.storage import (
    StorageBackend,
    StorageNotFoundError,
    StoragePreconditionFailedError,
)

logger = logging.getLogger(__name__)

REGISTRY_KEY = "datasets.json"
SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def validate_dataset_slug(slug: str) -> Tuple[bool, str]:
    """Validate that dataset name is a safe, lowercase URL slug."""
    slug = slug.strip()
    if not slug:
        return False, "Tên dataset không được để trống."
    if len(slug) < 3 or len(slug) > 50:
        return False, "Tên dataset phải từ 3 đến 50 ký tự."
    if not SLUG_PATTERN.match(slug):
        return False, "Tên dataset chỉ được chứa chữ cái thường (a-z), chữ số (0-9) và dấu gạch nối (-)."
    return True, ""


def get_registry(storage: StorageBackend) -> Tuple[DatasetsRegistry, str]:
    """Load the dataset registry and its current ETag."""
    try:
        data, etag = storage.get_object(REGISTRY_KEY)
        registry_data = json.loads(data.decode("utf-8"))
        return DatasetsRegistry(**registry_data), etag
    except StorageNotFoundError:
        raise StorageNotFoundError("Chưa tìm thấy tệp đăng ký dataset (datasets.json).")


def ensure_initialized(
    storage: StorageBackend,
    bootstrap_manifest_path: str = "manifest.jsonl",
) -> DatasetsRegistry:
    """Ensure the authoritative bucket is initialized with numeric-data-v1.

    If datasets.json already exists, does nothing and returns current registry.
    """
    head = storage.head_object(REGISTRY_KEY)
    if head is not None:
        reg, _ = get_registry(storage)
        return reg

    # Not initialized yet: read local bootstrap manifest
    if not os.path.exists(bootstrap_manifest_path):
        raise FileNotFoundError(f"Không tìm thấy file manifest mẫu tại '{bootstrap_manifest_path}'")

    with open(bootstrap_manifest_path, "rb") as f:
        manifest_bytes = f.read()

    items, errors = validate_manifest_content(manifest_bytes)
    if errors:
        raise ValueError(
            "Tệp manifest khởi tạo bị lỗi: " + "; ".join(errors[:5])
        )

    initial_slug = "numeric-data-v1"
    manifest_key = f"{initial_slug}/manifest.jsonl"
    config_key = f"{initial_slug}/config.json"

    # Upload initial manifest and config
    try:
        storage.put_object_if_none_match(
            manifest_key, manifest_bytes, content_type="application/x-ndjson"
        )
    except StoragePreconditionFailedError:
        pass  # Already written by concurrent worker

    config = DatasetConfig(
        name=initial_slug,
        display_name="Tập số tự nhiên v1 (500 câu)",
        task_count=len(items),
        created_at=utc_now_iso(),
        description="Bộ 500 kịch bản đọc số tự nhiên (CCCD, SĐT, STK) cho VietinBank ASR.",
    )

    try:
        storage.put_object_if_none_match(
            config_key,
            json.dumps(config.model_dump(), ensure_ascii=False, indent=2).encode("utf-8"),
            content_type="application/json",
        )
    except StoragePreconditionFailedError:
        pass

    registry = DatasetsRegistry(
        active_dataset=initial_slug,
        datasets={initial_slug: config},
        updated_at=utc_now_iso(),
    )

    reg_bytes = json.dumps(registry.model_dump(), ensure_ascii=False, indent=2).encode("utf-8")
    try:
        storage.put_object_if_none_match(
            REGISTRY_KEY, reg_bytes, content_type="application/json"
        )
        logger.info("Initialized %s with active dataset %s", REGISTRY_KEY, initial_slug)
        return registry
    except StoragePreconditionFailedError:
        # Race condition: another instance just initialized it
        reg, _ = get_registry(storage)
        return reg


def set_active_dataset(
    storage: StorageBackend,
    dataset_name: str,
    max_retries: int = 5,
) -> bool:
    """Atomically set the single active dataset for public readers."""
    for attempt in range(max_retries):
        registry, etag = get_registry(storage)
        if dataset_name not in registry.datasets:
            raise ValueError(f"Dataset '{dataset_name}' không tồn tại trong danh mục.")

        if registry.active_dataset == dataset_name:
            return True

        registry.active_dataset = dataset_name
        registry.updated_at = utc_now_iso()
        data = json.dumps(registry.model_dump(), ensure_ascii=False, indent=2).encode("utf-8")

        try:
            storage.put_object_if_match(
                REGISTRY_KEY, data, etag, content_type="application/json"
            )
            return True
        except StoragePreconditionFailedError:
            if attempt == max_retries - 1:
                raise
            continue

    return False


def create_dataset(
    storage: StorageBackend,
    name: str,
    display_name: str,
    manifest_bytes: bytes,
    description: str = "",
    max_retries: int = 5,
) -> Tuple[bool, str, List[str]]:
    """Create a new dataset namespace inside the bucket.

    Validates slug name, manifest rows, and performs atomic registration without
    switching the active dataset.
    """
    valid_slug, slug_msg = validate_dataset_slug(name)
    if not valid_slug:
        return False, slug_msg, []

    items, errors = validate_manifest_content(manifest_bytes)
    if errors:
        return False, f"Tệp manifest có {len(errors)} lỗi. Vui lòng sửa đổi trước khi tạo:", errors

    # Check collisions
    try:
        registry, _ = get_registry(storage)
        if name in registry.datasets:
            return False, f"Dataset với tên '{name}' đã tồn tại trong hệ thống.", []
    except StorageNotFoundError:
        pass

    manifest_key = f"{name}/manifest.jsonl"
    config_key = f"{name}/config.json"

    if storage.head_object(manifest_key) is not None:
        return False, f"Đã tồn tại dữ liệu cho dataset '{name}' trong kho lưu trữ.", []

    # Upload manifest and config
    try:
        storage.put_object_if_none_match(
            manifest_key, manifest_bytes, content_type="application/x-ndjson"
        )
    except StoragePreconditionFailedError:
        return False, f"Xảy ra xung đột: tệp {manifest_key} đã được tạo bởi tiến trình khác.", []

    config = DatasetConfig(
        name=name,
        display_name=display_name.strip() or name,
        task_count=len(items),
        created_at=utc_now_iso(),
        description=description.strip(),
    )

    try:
        storage.put_object_if_none_match(
            config_key,
            json.dumps(config.model_dump(), ensure_ascii=False, indent=2).encode("utf-8"),
            content_type="application/json",
        )
    except StoragePreconditionFailedError:
        storage.delete_object(manifest_key)
        return False, f"Xảy ra xung đột khi lưu cấu hình {config_key}.", []

    # Update registry with CAS retry
    for attempt in range(max_retries):
        try:
            reg, etag = get_registry(storage)
            if name in reg.datasets:
                return True, f"Tạo dataset '{name}' thành công ({len(items)} câu).", []

            reg.datasets[name] = config
            reg.updated_at = utc_now_iso()
            reg_bytes = json.dumps(reg.model_dump(), ensure_ascii=False, indent=2).encode("utf-8")

            storage.put_object_if_match(
                REGISTRY_KEY, reg_bytes, etag, content_type="application/json"
            )
            invalidate_manifest_cache(name)
            return True, f"Tạo dataset '{name}' thành công ({len(items)} câu).", []
        except StoragePreconditionFailedError:
            if attempt == max_retries - 1:
                # Cleanup partially created keys
                storage.delete_object(manifest_key)
                storage.delete_object(config_key)
                return False, "Không thể cập nhật danh mục dataset do xung đột ghi đồng thời.", []

    return False, "Không thể hoàn tất tạo dataset.", []

