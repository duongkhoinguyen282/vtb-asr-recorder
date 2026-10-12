"""Storage abstraction layer supporting private Hugging Face Storage Buckets via S3 API,
alongside a strict conditional-write in-memory Mock backend for local testing.
"""

from __future__ import annotations

import hashlib
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone
import threading
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
        """Fetch object data and its ETag. Raises StorageNotFoundError if missing."""
        pass

    @abstractmethod
    def put_object(
        self,
        key: str,
        data: bytes,
        content_type: str = "application/octet-stream",
    ) -> str:
        """Unconditionally put an object. Returns the resulting ETag."""
        pass

    @abstractmethod
    def put_object_if_none_match(
        self,
        key: str,
        data: bytes,
        content_type: str = "application/json",
    ) -> str:
        """Put object ONLY if it does not already exist (CAS for creation).

        Raises StoragePreconditionFailedError if key already exists.
        Returns the resulting ETag.
        """
        pass

    @abstractmethod
    def put_object_if_match(
        self,
        key: str,
        data: bytes,
        etag: str,
        content_type: str = "application/json",
    ) -> str:
        """Put object ONLY if current object's ETag matches the provided etag (CAS for update).

        Raises StoragePreconditionFailedError if etag does not match or key is missing.
        Returns the new ETag.
        """
        pass

    @abstractmethod
    def head_object(self, key: str) -> Optional[Dict[str, Any]]:
        """Return metadata dict if key exists, else None."""
        pass

    @abstractmethod
    def delete_object(self, key: str) -> bool:
        """Delete an object. Return True if deleted, False if did not exist."""
        pass

    @abstractmethod
    def list_objects(self, prefix: str = "") -> List[Dict[str, Any]]:
        """List objects matching prefix. Returns list of dicts with Key, ETag, Size, LastModified."""
        pass


def _calc_etag(data: bytes) -> str:
    """Calculate an S3-compatible quoted MD5 ETag for bytes."""
    md5_hash = hashlib.md5(data).hexdigest()
    return f'"{md5_hash}"'


class MockStorage(StorageBackend):
    """In-memory storage adapter with exact conditional-write semantics.

    DEVELOPMENT / TESTING ONLY. Never silently enabled in production.
    """

    def __init__(self, bucket_name: str = "vtb-recording-data"):
        self.bucket_name = bucket_name
        self._objects: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.RLock()
        logger.warning(
            "DEVELOPMENT ONLY: MockStorage is active. Authoritative data is stored in memory and will NOT persist across server restarts."
        )

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
                    f"Precondition failed (If-None-Match): key '{key}' already exists with ETag {self._objects[key]['etag']}"
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
            # S3 ETags may or may not include quotes in comparison
            norm_current = current_etag.strip('"')
            norm_target = etag.strip('"')
            if norm_current != norm_target:
                raise StoragePreconditionFailedError(
                    f"Precondition failed (If-Match): ETag mismatch on key '{key}'. Expected '{etag}', current is '{current_etag}'"
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
            results = []
            for key, obj in self._objects.items():
                if key.startswith(prefix):
                    results.append(
                        {
                            "Key": key,
                            "ETag": obj["etag"],
                            "Size": obj["size"],
                            "LastModified": obj["last_modified"],
                        }
                    )
            return results


class HuggingFaceS3Storage(StorageBackend):
    """Production S3 storage adapter connecting to Hugging Face Storage Buckets.

    Uses boto3 with path-style addressing and endpoint https://s3.hf.co/<HF_NAMESPACE>.
    """

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

    def get_object(self, key: str) -> Tuple[bytes, str]:
        try:
            resp = self._s3.get_object(Bucket=self.bucket_name, Key=key)
            data = resp["Body"].read()
            etag = resp.get("ETag", "")
            return data, etag
        except self._ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("NoSuchKey", "404", "NotFound"):
                raise StorageNotFoundError(f"Object not found: {key}") from exc
            raise StorageError(f"S3 get_object error for {key}: {exc}") from exc

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
            return resp.get("ETag", "")
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
            return resp.get("ETag", "")
        except self._ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if status in (409, 412) or code in ("PreconditionFailed", "412", "ConditionalCheckFailed", "ConditionalRequestConflict", "Conflict", "409"):
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
        # Standardize etag formatting for header
        clean_etag = etag if etag.startswith('"') else f'"{etag}"'
        try:
            resp = self._s3.put_object(
                Bucket=self.bucket_name,
                Key=key,
                Body=data,
                ContentType=content_type,
                IfMatch=clean_etag,
            )
            return resp.get("ETag", "")
        except self._ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if status in (409, 412) or code in ("PreconditionFailed", "412", "ConditionalCheckFailed", "ConditionalRequestConflict", "Conflict", "409"):
                raise StoragePreconditionFailedError(
                    f"Conditional write failed (IfMatch={clean_etag}) on {key}"
                ) from exc
            if code in ("NoSuchKey", "404", "NotFound"):
                raise StoragePreconditionFailedError(f"Conditional update target missing: {key}") from exc
            raise StorageError(f"S3 put_object_if_match error for {key}: {exc}") from exc

    def head_object(self, key: str) -> Optional[Dict[str, Any]]:
        try:
            resp = self._s3.head_object(Bucket=self.bucket_name, Key=key)
            return resp
        except self._ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("NoSuchKey", "404", "NotFound"):
                return None
            raise StorageError(f"S3 head_object error for {key}: {exc}") from exc

    def delete_object(self, key: str) -> bool:
        try:
            self._s3.delete_object(Bucket=self.bucket_name, Key=key)
            return True
        except self._ClientError as exc:
            logger.warning("Failed to delete object %s: %s", key, exc)
            return False

    def list_objects(self, prefix: str = "") -> List[Dict[str, Any]]:
        try:
            results: List[Dict[str, Any]] = []
            paginator = self._s3.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self.bucket_name, Prefix=prefix):
                contents = page.get("Contents", [])
                for item in contents:
                    results.append(
                        {
                            "Key": item["Key"],
                            "ETag": item.get("ETag", ""),
                            "Size": item.get("Size", 0),
                            "LastModified": item.get("LastModified"),
                        }
                    )
            return results
        except self._ClientError as exc:
            raise StorageError(f"S3 list_objects error for prefix {prefix}: {exc}") from exc


_GLOBAL_STORAGE_INSTANCE: Optional[StorageBackend] = None
_GLOBAL_LOCK = threading.Lock()


def get_storage(secrets_dict: Optional[Dict[str, Any]] = None) -> StorageBackend:
    """Factory function returning the configured storage backend.

    Reads configuration from secrets_dict or streamlit.secrets or os.environ.
    Uses MockStorage if MOCK_STORAGE is true or if HF credentials are missing.
    """
    global _GLOBAL_STORAGE_INSTANCE
    with _GLOBAL_LOCK:
        if _GLOBAL_STORAGE_INSTANCE is not None and secrets_dict is None:
            return _GLOBAL_STORAGE_INSTANCE

        config: Dict[str, Any] = {}
        # Try importing streamlit to check st.secrets
        try:
            import streamlit as st
            if hasattr(st, "secrets"):
                for k in (
                    "HF_NAMESPACE",
                    "HF_BUCKET_NAME",
                    "HF_S3_ACCESS_KEY_ID",
                    "HF_S3_SECRET_ACCESS_KEY",
                    "ADMIN_PASSWORD",
                    "MOCK_STORAGE",
                ):
                    if k in st.secrets:
                        config[k] = st.secrets[k]
        except Exception:
            pass

        import os
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
                logger.warning(
                    "HF S3 credentials not fully provided. Falling back to in-memory MockStorage."
                )
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
    """Reset cached global storage instance (useful for unit tests)."""
    global _GLOBAL_STORAGE_INSTANCE
    with _GLOBAL_LOCK:
        _GLOBAL_STORAGE_INSTANCE = None

