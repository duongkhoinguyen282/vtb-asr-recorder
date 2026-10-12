from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier, current_thread
import random

import pytest

from services import tasks
from services.storage import MockStorage, StoragePreconditionFailedError
from tests.test_audio import make_dummy_wav
from tests.test_concurrency_and_leases import make_test_manifest


def claim(store, session="reader", manifest=None):
    return tasks.allocate_task(store, "test", manifest or make_test_manifest(1), session, "male")


def submit(store, item, state, session="reader", token="attempt", audio=None):
    return tasks.submit_recording(store, "test", item.id, state.recording_lease.lease_id,
                                  session, audio or make_dummy_wav(duration=1), "male",
                                  idempotency_token=token)


def test_same_identity_simultaneous_tabs_get_one_lease(monkeypatch):
    store = MockStorage()
    barrier = Barrier(2)
    original = tasks.load_all_task_states

    def snapshot(*args):
        result = original(*args)
        barrier.wait(timeout=5)
        return result

    def shuffle(items):
        if current_thread().name.endswith("_1"):
            items.reverse()

    monkeypatch.setattr(tasks, "load_all_task_states", snapshot)
    monkeypatch.setattr(random, "shuffle", shuffle)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: claim(store, manifest=make_test_manifest(6)), range(2)))
    assert results[0][0].id == results[1][0].id
    assert results[0][1].recording_lease.lease_id == results[1][1].recording_lease.lease_id


@pytest.mark.parametrize("existing", [False, True])
def test_atomic_claim_exactly_one_winner(existing):
    store = MockStorage()
    etag = store.put_object("state", b"available") if existing else None
    barrier = Barrier(2)

    def write(i):
        barrier.wait(timeout=5)
        try:
            if existing:
                store.put_object_if_match("state", str(i).encode(), etag)
            else:
                store.put_object_if_none_match("state", str(i).encode())
            return True
        except StoragePreconditionFailedError:
            return False

    with ThreadPoolExecutor(2) as pool:
        assert sum(pool.map(write, range(2))) == 1


def test_independent_concurrent_claims_and_submissions():
    store = MockStorage()
    manifest = make_test_manifest(2)
    barrier = Barrier(2)

    def allocate(i):
        barrier.wait(timeout=5)
        return claim(store, str(i), manifest)

    with ThreadPoolExecutor(2) as pool:
        assignments = list(pool.map(allocate, range(2)))
        assert assignments[0][0].id != assignments[1][0].id
        results = list(pool.map(lambda i: submit(store, *assignments[i][:2], session=str(i)), range(2)))
    assert all(result[0] for result in results)
    assert claim(store, "third", manifest)[0] is None


def test_allocator_moves_to_another_candidate_after_atomic_loss(monkeypatch):
    store = MockStorage()
    original = store.put_object_if_none_match
    raced = []

    def competing_claim(key, data, **kwargs):
        if not raced:
            import json
            value = json.loads(data)
            value["recording_lease"]["session_id"] = "other-reader"
            value["recording_lease"]["lease_id"] = "other-lease"
            raced.append(key)
            original(key, json.dumps(value).encode(), **kwargs)
        return original(key, data, **kwargs)

    monkeypatch.setattr(store, "put_object_if_none_match", competing_claim)
    item, state, _ = claim(store, manifest=make_test_manifest(2))
    assert item is not None
    assert f"test/states/{item.id}.json" != raced[0]
    assert state.recording_lease.session_id == "reader"


def test_wrong_session_cannot_submit_even_with_lease_id():
    store = MockStorage()
    item, state, _ = claim(store)
    assert not submit(store, item, state, session="intruder")[0]


def test_identical_audio_does_not_acknowledge_other_participants_submission():
    store = MockStorage()
    item, state, _ = claim(store)
    assert submit(store, item, state)[0]
    assert not submit(store, item, state, session="intruder")[0]


def test_retry_token_cannot_overwrite_uploaded_audio(monkeypatch):
    store = MockStorage()
    item, state, _ = claim(store)
    original = store.put_object_if_match
    monkeypatch.setattr(store, "put_object_if_match", lambda *args, **kwargs: (_ for _ in ()).throw(StoragePreconditionFailedError()))
    assert not submit(store, item, state)[0]
    key = store.list_objects("test/audio/")[0]["Key"]
    before = store.get_object(key)[0]
    monkeypatch.setattr(store, "put_object_if_match", original)
    assert not submit(store, item, state, audio=make_dummy_wav(duration=2))[0]
    assert store.get_object(key)[0] == before


def test_lease_expiring_during_upload_cannot_commit(monkeypatch):
    store = MockStorage()
    item, state, _ = claim(store)
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    real_get = store.get_object

    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return future

    def get(key):
        result = real_get(key)
        if "/audio/" in key:
            monkeypatch.setattr(tasks, "datetime", Later)
        return result

    # The commit must recheck time after read-back (head alone is insufficient).
    real_head = store.head_object
    def head(key):
        result = real_head(key)
        if "/audio/" in key:
            monkeypatch.setattr(tasks, "datetime", Later)
        return result
    monkeypatch.setattr(store, "get_object", get)
    monkeypatch.setattr(store, "head_object", head)
    assert not submit(store, item, state)[0]
