"""Storage abstraction layer supporting private Hugging Face Storage Buckets via S3 API,
with high-speed in-memory caching for JSON state/metadata files and parallel warmup.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import logging
import os
import threading
import time
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class StorageError(Exception):
    """Base storage error."""
    pass


class StorageNotFoundError(StorageError):
    """Raised when an object key is not found."""
    pass


class StoragePreconditionFailedError(StorageError):
    """Raised when conditional put (If-Match or If-None-Match) fails."""
    pass


class StorageBackend(ABC):
    """Abstract interface for object storage."""

    @abstractmethod
    def get_object(self, key: str) -> Tuple[bytes, str]:
        pass

    @abstractmethod
    def put_object(
        self,
        key: str,
        data: bytes,
        content_type: str = "application/octet-stream",
    ) -> str:
        pass

    @abstractmethod
    def put_object_if_none_match(
        self,
        key: str,
        data: bytes,
        content_type: str = "application/json",
    ) -> str:
        pass

    @abstractmethod
    def put_object_if_match(
        self,
        key: str,
        data: bytes,
        etag: str,
        content_type: str = "application/json",
    ) -> str:
        pass

    @abstractmethod
    def head_object(self, key: str) -> Optional[Dict[str, Any]]:
        pass

    @abstractmethod
    def delete_object(self, key: str) -> bool:
        pass

    @abstractmethod
    def list_objects(self, prefix: str = "") -> List[Dict[str, Any]]:
        pass


def _calc_etag(data: bytes) -> str:
    md5_hash = hashlib.md5(data).hexdigest()
    return f'"{md5_hash}"'


def _is_cacheable_key(key: str) -> bool:
    """Cache small metadata/state files in RAM; do not cache heavy .wav files in RAM."""
    return key.endswith(".json") or key.endswith(".jsonl")


class MockStorage(StorageBackend):
    """In-memory storage adapter with exact conditional-write semantics."""

    def __init__(self, bucket_name: str = "vtb-recording-data"):
        self.bucket_name = bucket_name
        self._objects: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.RLock()
        logger.warning("DEVELOPMENT ONLY: MockStorage is active in memory.")

    def get_object(self, key: str) -> Tuple[bytes, str]:
        with self._lock:
            if key not in self._objects:
                raise StorageNotFoundError(f"Object not found: {key}")
            obj = self._objects[key]
            return bytes(obj["data"]), str(obj["etag"])

    def put_object(
        self,
        key: str,
        data: bytes,
        content_type: str = "application/octet-stream",
    ) -> str:
        with self._lock:
            etag = _calc_etag(data)
            self._objects[key] = {
                "data": bytes(data),
                "etag": etag,
                "content_type": content_type,
                "size": len(data),
                "last_modified": datetime.now(timezone.utc),
            }
            return etag

    def put_object_if_none_match(
        self,
        key: str,
        data: bytes,
        content_type: str = "application/json",
    ) -> str:
        with self._lock:
            if key in self._objects:
                raise StoragePreconditionFailedError(
                    f"Precondition failed (If-None-Match): key '{key}' already exists"
                )
            return self.put_object(key, data, content_type)

    def put_object_if_match(
        self,
        key: str,
        data: bytes,
        etag: str,
        content_type: str = "application/json",
    ) -> str:
        with self._lock:
            if key not in self._objects:
                raise StoragePreconditionFailedError(
                    f"Precondition failed (If-Match): key '{key}' does not exist"
                )
            current_etag = self._objects[key]["etag"]
            if current_etag.strip('"') != etag.strip('"'):
                raise StoragePreconditionFailedError(
                    f"Precondition failed (If-Match): ETag mismatch on key '{key}'"
                )
            return self.put_object(key, data, content_type)

    def head_object(self, key: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            if key not in self._objects:
                return None
            obj = self._objects[key]
            return {
                "ETag": obj["etag"],
                "ContentLength": obj["size"],
                "ContentType": obj["content_type"],
                "LastModified": obj["last_modified"],
            }

    def delete_object(self, key: str) -> bool:
        with self._lock:
            if key in self._objects:
                del self._objects[key]
                return True
            return False

    def list_objects(self, prefix: str = "") -> List[Dict[str, Any]]:
        with self._lock:
            return [
                {
                    "Key": key,
                    "ETag": obj["etag"],
                    "Size": obj["size"],
                    "LastModified": obj["last_modified"],
                }
                for key, obj in self._objects.items()
                if key.startswith(prefix)
            ]


class HuggingFaceS3Storage(StorageBackend):
    """Production S3 storage adapter with synchronized RAM cache for state/metadata files."""

    def __init__(
        self,
        namespace: str,
        bucket_name: str,
        access_key_id: str,
        secret_access_key: str,
    ):
        import boto3
        import botocore.client
        from botocore.exceptions import ClientError

        self.namespace = namespace
        self.bucket_name = bucket_name
        self._ClientError = ClientError

        endpoint_url = f"https://s3.hf.co/{namespace}"
        boto_config = botocore.client.Config(
            s3={"addressing_style": "path"},
            region_name="us-east-1",
            max_pool_connections=32,
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
            retries={"max_attempts": 3, "mode": "standard"},
        )

        self._s3 = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            region_name="us-east-1",
            config=boto_config,
        )

        self._cache_lock = threading.RLock()
        self._data_cache: Dict[str, Tuple[bytes, str]] = {}
        self._meta_cache: Dict[str, Dict[str, Any]] = {}
        self._warmed_prefixes: Dict[str, float] = {}
        self._PREFIX_SYNC_TTL = 45.0

    def _fetch_from_s3(self, key: str) -> Tuple[bytes, str]:
        try:
            resp = self._s3.get_object(Bucket=self.bucket_name, Key=key)
            data = resp["Body"].read()
            etag = resp.get("ETag", "")
            if _is_cacheable_key(key):
                with self._cache_lock:
                    self._data_cache[key] = (data, etag)
                    self._meta_cache[key] = {
                        "Key": key,
                        "ETag": etag,
                        "Size": len(data),
                        "LastModified": resp.get("LastModified") or datetime.now(timezone.utc),
                    }
            return data, etag
        except self._ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("NoSuchKey", "404", "NotFound"):
                with self._cache_lock:
                    self._data_cache.pop(key, None)
                    self._meta_cache.pop(key, None)
                raise StorageNotFoundError(f"Object not found: {key}") from exc
            raise StorageError(f"S3 get_object error for {key}: {exc}") from exc

    def get_object(self, key: str) -> Tuple[bytes, str]:
        if _is_cacheable_key(key):
            with self._cache_lock:
                if key in self._data_cache:
                    return self._data_cache[key]
        return self._fetch_from_s3(key)

    def put_object(
        self,
        key: str,
        data: bytes,
        content_type: str = "application/octet-stream",
    ) -> str:
        try:
            resp = self._s3.put_object(
                Bucket=self.bucket_name,
                Key=key,
                Body=data,
                ContentType=content_type,
            )
            etag = resp.get("ETag", "")
            if _is_cacheable_key(key):
                with self._cache_lock:
                    self._data_cache[key] = (bytes(data), etag)
                    self._meta_cache[key] = {
                        "Key": key,
                        "ETag": etag,
                        "Size": len(data),
                        "LastModified": datetime.now(timezone.utc),
                    }
            return etag
        except self._ClientError as exc:
            raise StorageError(f"S3 put_object error for {key}: {exc}") from exc

    def put_object_if_none_match(
        self,
        key: str,
        data: bytes,
        content_type: str = "application/json",
    ) -> str:
        try:
            resp = self._s3.put_object(
                Bucket=self.bucket_name,
                Key=key,
                Body=data,
                ContentType=content_type,
                IfNoneMatch="*",
            )
            etag = resp.get("ETag", "")
            if _is_cacheable_key(key):
                with self._cache_lock:
                    self._data_cache[key] = (bytes(data), etag)
                    self._meta_cache[key] = {
                        "Key": key,
                        "ETag": etag,
                        "Size": len(data),
                        "LastModified": datetime.now(timezone.utc),
                    }
            return etag
        except self._ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if status in (409, 412) or code in (
                "PreconditionFailed", "412", "ConditionalCheckFailed",
                "ConditionalRequestConflict", "Conflict", "409"
            ):
                if _is_cacheable_key(key):
                    try:
                        self._fetch_from_s3(key)
                    except Exception:
                        pass
                raise StoragePreconditionFailedError(
                    f"Conditional write failed (IfNoneMatch=*): {key} already exists"
                ) from exc
            raise StorageError(f"S3 put_object_if_none_match error for {key}: {exc}") from exc

    def put_object_if_match(
        self,
        key: str,
        data: bytes,
        etag: str,
        content_type: str = "application/json",
    ) -> str:
        clean_etag = etag if etag.startswith('"') else f'"{etag}"'
        try:
            resp = self._s3.put_object(
                Bucket=self.bucket_name,
                Key=key,
                Body=data,
                ContentType=content_type,
                IfMatch=clean_etag,
            )
            new_etag = resp.get("ETag", "")
            if _is_cacheable_key(key):
                with self._cache_lock:
                    self._data_cache[key] = (bytes(data), new_etag)
                    self._meta_cache[key] = {
                        "Key": key,
                        "ETag": new_etag,
                        "Size": len(data),
                        "LastModified": datetime.now(timezone.utc),
                    }
            return new_etag
        except self._ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if status in (409, 412) or code in (
                "PreconditionFailed", "412", "ConditionalCheckFailed",
                "ConditionalRequestConflict", "Conflict", "409"
            ):
                if _is_cacheable_key(key):
                    try:
                        self._fetch_from_s3(key)
                    except Exception:
                        pass
                raise StoragePreconditionFailedError(
                    f"Conditional write failed (IfMatch={clean_etag}) on {key}"
                ) from exc
            if code in ("NoSuchKey", "404", "NotFound"):
                with self._cache_lock:
                    self._data_cache.pop(key, None)
                    self._meta_cache.pop(key, None)
                raise StoragePreconditionFailedError(f"Conditional update target missing: {key}") from exc
            raise StorageError(f"S3 put_object_if_match error for {key}: {exc}") from exc

    def head_object(self, key: str) -> Optional[Dict[str, Any]]:
        if _is_cacheable_key(key):
            with self._cache_lock:
                if key in self._meta_cache:
                    meta = self._meta_cache[key]
                    return {
                        "ETag": meta["ETag"],
                        "ContentLength": meta["Size"],
                        "LastModified": meta["LastModified"],
                    }
        try:
            resp = self._s3.head_object(Bucket=self.bucket_name, Key=key)
            return resp
        except self._ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("NoSuchKey", "404", "NotFound"):
                return None
            raise StorageError(f"S3 head_object error for {key}: {exc}") from exc

    def delete_object(self, key: str) -> bool:
        with self._cache_lock:
            self._data_cache.pop(key, None)
            self._meta_cache.pop(key, None)
        try:
            self._s3.delete_object(Bucket=self.bucket_name, Key=key)
            return True
        except self._ClientError as exc:
            logger.warning("Failed to delete object %s: %s", key, exc)
            return False

    def list_objects(self, prefix: str = "") -> List[Dict[str, Any]]:
        now = time.monotonic()
        with self._cache_lock:
            last_sync = self._warmed_prefixes.get(prefix, 0.0)
            if (now - last_sync) < self._PREFIX_SYNC_TTL:
                return [
                    dict(meta)
                    for k, meta in self._meta_cache.items()
                    if k.startswith(prefix)
                ]

        try:
            s3_items: List[Dict[str, Any]] = []
            paginator = self._s3.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self.bucket_name, Prefix=prefix):
                for item in page.get("Contents", []):
                    s3_items.append(
                        {
                            "Key": item["Key"],
                            "ETag": item.get("ETag", ""),
                            "Size": item.get("Size", 0),
                            "LastModified": item.get("LastModified"),
                        }
                    )

            keys_to_fetch: List[str] = []
            s3_key_set = set()

            with self._cache_lock:
                for item in s3_items:
                    k = item["Key"]
                    s3_key_set.add(k)
                    self._meta_cache[k] = item
                    if _is_cacheable_key(k):
                        cached = self._data_cache.get(k)
                        if not cached or cached[1].strip('"') != item["ETag"].strip('"'):
                            keys_to_fetch.append(k)

                stale_keys = [
                    k for k in self._meta_cache
                    if k.startswith(prefix) and k not in s3_key_set
                ]
                for k in stale_keys:
                    self._meta_cache.pop(k, None)
                    self._data_cache.pop(k, None)

            if keys_to_fetch:
                with ThreadPoolExecutor(max_workers=16) as pool:
                    list(pool.map(self._fetch_from_s3, keys_to_fetch))

            with self._cache_lock:
                self._warmed_prefixes[prefix] = time.monotonic()

            return s3_items
        except self._ClientError as exc:
            raise StorageError(f"S3 list_objects error for prefix {prefix}: {exc}") from exc


_GLOBAL_STORAGE_INSTANCE: Optional[StorageBackend] = None
_GLOBAL_LOCK = threading.Lock()


def get_storage(secrets_dict: Optional[Dict[str, Any]] = None) -> StorageBackend:
    """Factory function returning the configured storage backend."""
    global _GLOBAL_STORAGE_INSTANCE
    with _GLOBAL_LOCK:
        if _GLOBAL_STORAGE_INSTANCE is not None and secrets_dict is None:
            return _GLOBAL_STORAGE_INSTANCE

        config: Dict[str, Any] = {}
        if os.path.exists(".env"):
            with open(".env", "r", encoding="utf-8") as ef:
                for line in ef:
                    if "=" in line and not line.strip().startswith("#"):
                        k_env, v_env = line.strip().split("=", 1)
                        os.environ.setdefault(k_env.strip(), v_env.strip().strip('"').strip("'"))
        for k in (
            "HF_NAMESPACE",
            "HF_BUCKET_NAME",
            "HF_S3_ACCESS_KEY_ID",
            "HF_S3_SECRET_ACCESS_KEY",
            "ADMIN_PASSWORD",
            "MOCK_STORAGE",
        ):
            if k in os.environ:
                config[k] = os.environ[k]

        if secrets_dict:
            config.update(secrets_dict)

        mock_requested = str(config.get("MOCK_STORAGE", "false")).lower() in ("true", "1", "yes")
        namespace = config.get("HF_NAMESPACE")
        bucket_name = config.get("HF_BUCKET_NAME", "vtb-recording-data")
        access_key = config.get("HF_S3_ACCESS_KEY_ID")
        secret_key = config.get("HF_S3_SECRET_ACCESS_KEY")

        if mock_requested or not (namespace and access_key and secret_key):
            if not mock_requested:
                logger.warning("HF S3 credentials not fully provided. Using MockStorage.")
            backend = MockStorage(bucket_name=bucket_name)
        else:
            backend = HuggingFaceS3Storage(
                namespace=namespace,
                bucket_name=bucket_name,
                access_key_id=access_key,
                secret_access_key=secret_key,
            )

        if secrets_dict is None:
            _GLOBAL_STORAGE_INSTANCE = backend
        return backend


def reset_global_storage() -> None:
    """Reset cached global storage instance."""
    global _GLOBAL_STORAGE_INSTANCE
    with _GLOBAL_LOCK:
        _GLOBAL_STORAGE_INSTANCE = None
