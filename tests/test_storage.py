"""Tests for storage layer and strict conditional-write semantics."""

import pytest
from services.storage import (
    HuggingFaceS3Storage,
    MockStorage,
    StorageNotFoundError,
    StoragePreconditionFailedError,
    get_storage,
)


def test_mock_storage_put_and_get():
    """Verify basic get/put in MockStorage."""
    storage = MockStorage()
    data = b'{"hello": "world"}'
    etag = storage.put_object("test/item.json", data, content_type="application/json")
    assert etag.startswith('"') and etag.endswith('"')

    read_data, read_etag = storage.get_object("test/item.json")
    assert read_data == data
    assert read_etag == etag


def test_mock_storage_put_if_none_match():
    """Verify If-None-Match conditional write semantics."""
    storage = MockStorage()
    key = "state/HNUM-0001.json"

    # First write must succeed
    etag1 = storage.put_object_if_none_match(key, b'{"status": "reserved"}')
    assert etag1

    # Second write with If-None-Match on existing key must fail with PreconditionFailed
    with pytest.raises(StoragePreconditionFailedError):
        storage.put_object_if_none_match(key, b'{"status": "stolen"}')


def test_mock_storage_put_if_match():
    """Verify If-Match conditional write semantics."""
    storage = MockStorage()
    key = "state/HNUM-0002.json"

    etag1 = storage.put_object(key, b'{"v": 1}')

    # Writing with wrong etag must fail
    with pytest.raises(StoragePreconditionFailedError):
        storage.put_object_if_match(key, b'{"v": 2}', etag='"wrong-etag"')

    # Writing with correct etag must succeed and return new etag
    etag2 = storage.put_object_if_match(key, b'{"v": 2}', etag=etag1)
    assert etag2 != etag1

    read_data, read_etag = storage.get_object(key)
    assert read_data == b'{"v": 2}'
    assert read_etag == etag2


def test_mock_storage_get_not_found():
    """Verify get_object on non-existent key raises StorageNotFoundError."""
    storage = MockStorage()
    with pytest.raises(StorageNotFoundError):
        storage.get_object("nonexistent.json")


def test_mock_storage_list_and_delete():
    """Verify listing with prefix and deletion."""
    storage = MockStorage()
    storage.put_object("prefix/a.txt", b"A")
    storage.put_object("prefix/b.txt", b"B")
    storage.put_object("other/c.txt", b"C")

    items = storage.list_objects(prefix="prefix/")
    keys = [it["Key"] for it in items]
    assert len(keys) == 2
    assert "prefix/a.txt" in keys
    assert "prefix/b.txt" in keys

    deleted = storage.delete_object("prefix/a.txt")
    assert deleted is True
    assert len(storage.list_objects(prefix="prefix/")) == 1


def test_hf_s3_storage_initialization():
    """Verify HuggingFaceS3Storage builds correct S3 endpoint and path addressing."""
    storage = HuggingFaceS3Storage(
        namespace="my-org",
        bucket_name="vtb-recording-data",
        access_key_id="test-key",
        secret_access_key="test-secret",
    )
    assert storage.namespace == "my-org"
    assert storage.bucket_name == "vtb-recording-data"
    assert "https://s3.hf.co/my-org" in str(storage._s3.meta.endpoint_url)


def test_get_storage_factory():
    """Verify factory returns MockStorage when credentials are mock or missing."""
    mock_store = get_storage({"MOCK_STORAGE": "true"})
    assert isinstance(mock_store, MockStorage)


@pytest.mark.parametrize("method", ["put_object_if_match", "put_object_if_none_match"])
def test_s3_conditional_request_conflict_is_retryable(method):
    from botocore.stub import Stubber
    storage = HuggingFaceS3Storage("test", "test-bucket", "fake-key", "fake-secret")
    with Stubber(storage._s3) as stub:
        stub.add_client_error("put_object", service_error_code="ConditionalRequestConflict",
                              http_status_code=409)
        with pytest.raises(StoragePreconditionFailedError):
            if method == "put_object_if_match":
                storage.put_object_if_match("key", b"value", '"etag"')
            else:
                storage.put_object_if_none_match("key", b"value")


def test_s3_passes_conditional_headers_and_etags():
    from botocore.stub import Stubber
    storage = HuggingFaceS3Storage("test", "test-bucket", "fake-key", "fake-secret")
    common = dict(Bucket="test-bucket", Key="key", Body=b"value", ContentType="application/json")
    with Stubber(storage._s3) as stub:
        stub.add_response("put_object", {"ETag": '"first"'}, dict(common, IfNoneMatch="*"))
        stub.add_response("put_object", {"ETag": '"second"'}, dict(common, IfMatch='"first"'))
        assert storage.put_object_if_none_match("key", b"value") == '"first"'
        assert storage.put_object_if_match("key", b"value", '"first"') == '"second"'

