"""Exercise real Streamlit reruns; only microphone capture is substituted."""
from io import BytesIO

import pytest
from streamlit.testing.v1 import AppTest

from services import storage as storage_module
from services import tasks
from services.manifest import invalidate_manifest_cache
from tests.test_audio import make_dummy_wav


@pytest.fixture
def reader_backend(monkeypatch):
    storage_module.reset_global_storage()
    monkeypatch.setenv("MOCK_STORAGE", "true")
    invalidate_manifest_cache()
    backend = storage_module.get_storage()
    yield backend
    storage_module.reset_global_storage()
    invalidate_manifest_cache()


def open_reader():
    app = AppTest.from_file("app.py", default_timeout=15).run()
    assert not app.exception
    app.selectbox[0].select("Nam").run()
    assert not app.exception
    return app


def test_independent_ui_sessions_share_backend_and_survive_reruns(reader_backend):
    first, second = open_reader(), open_reader()
    assert first.session_state.reader_session_id != second.session_state.reader_session_id
    assert first.session_state.current_task_id != second.session_state.current_task_id
    task = first.session_state.current_task_id
    for _ in range(3):
        first.run()
        assert not first.exception
        assert first.session_state.current_task_id == task
        assert storage_module.get_storage() is reader_backend
    refreshed = AppTest.from_file("app.py", default_timeout=15)
    refreshed.query_params.update(first.query_params)
    refreshed.run()
    assert not refreshed.exception
    assert refreshed.session_state.current_task_id == task


def test_lost_reservation_does_not_replace_widget_or_claim_no_audio(reader_backend, monkeypatch):
    import streamlit as st
    wav = make_dummy_wav(duration=1)
    captured = {}

    def microphone(label, key, **kwargs):
        # A real widget returns no file if its key changes on the submit rerun.
        captured.setdefault("key", key)
        return BytesIO(wav) if key == captured["key"] else None

    monkeypatch.setattr(st, "audio_input", microphone)
    app = open_reader()
    original = app.session_state.current_task_id
    state, _ = tasks.get_task_state(reader_backend, "numeric-data-v1", original)
    tasks.release_lease(reader_backend, "numeric-data-v1", original, state.recording_lease.lease_id)
    app.button[0].click().run()
    assert not app.exception
    assert app.session_state.current_task_id == original
    assert not any("Bạn chưa thu âm" in warning.value for warning in app.warning)
    assert app.error
    assert app.session_state["reader_attempt"]["audio"] == wav


def test_recording_waits_for_explicit_next_after_commit(reader_backend, monkeypatch):
    import streamlit as st
    monkeypatch.setattr(st, "audio_input", lambda *args, **kwargs: BytesIO(make_dummy_wav(duration=1)))
    app = open_reader()
    task = app.session_state.current_task_id
    app.button[0].click().run()
    assert not app.exception
    app.run()
    assert not app.exception
    assert app.session_state.current_task_id == task
    assert len(reader_backend.list_objects("numeric-data-v1/audio/")) == 1


@pytest.mark.parametrize("after_commit", [False, True])
def test_upload_timeout_preserves_audio_and_retry_is_safe(reader_backend, monkeypatch, after_commit):
    import streamlit as st
    from services.storage import StorageError
    wav = make_dummy_wav(duration=1)
    monkeypatch.setattr(st, "audio_input", lambda *args, **kwargs: BytesIO(wav))
    app = open_reader()
    task = app.session_state.current_task_id
    if after_commit:
        method = "put_object_if_match"
    else:
        method = "put_object_if_none_match"
    original = getattr(reader_backend, method)

    def timeout(key, *args, **kwargs):
        if after_commit and "/states/" in key and b'"recorded"' in args[0]:
            original(key, *args, **kwargs)
            raise StorageError("Lost commit acknowledgement")
        if not after_commit and "/audio/" in key:
            raise StorageError("Upload failed")
        return original(key, *args, **kwargs)

    monkeypatch.setattr(reader_backend, method, timeout)
    app.button[0].click().run()
    assert not app.exception
    assert app.error
    assert not app.session_state["reader_attempt"]["committed"]
    assert app.session_state["reader_attempt"]["audio"] == wav
    monkeypatch.setattr(reader_backend, method, original)
    # Simulate a missing widget value on retry: explicit session bytes survive.
    monkeypatch.setattr(st, "audio_input", lambda *args, **kwargs: None)
    app.button[0].click().run()
    assert not app.exception
    assert app.session_state["reader_attempt"]["committed"]
    assert app.session_state.current_task_id == task
    assert len(reader_backend.list_objects("numeric-data-v1/audio/")) == 1


def test_dataset_switch_does_not_move_inflight_recording(reader_backend, monkeypatch):
    import streamlit as st
    from services.dataset_registry import create_dataset, set_active_dataset
    from tests.test_concurrency_and_leases import make_test_manifest
    monkeypatch.setattr(st, "audio_input", lambda *args, **kwargs: BytesIO(make_dummy_wav(duration=1)))
    app = open_reader()
    task = app.session_state.current_task_id
    manifest = "\n".join(item.model_dump_json() for item in make_test_manifest(2)).encode()
    assert create_dataset(reader_backend, "other-data", "Other", manifest)[0]
    set_active_dataset(reader_backend, "other-data")
    refreshed = AppTest.from_file("app.py", default_timeout=15)
    refreshed.query_params.update(app.query_params)
    refreshed.run()
    assert not refreshed.exception
    assert refreshed.session_state["reader_attempt"]["dataset"] == "numeric-data-v1"
    assert refreshed.session_state.current_task_id == task
    app.button[0].click().run()
    assert not app.exception
    assert app.session_state["reader_attempt"]["committed"]
    assert app.session_state["reader_attempt"]["dataset"] == "numeric-data-v1"
    assert app.session_state.current_task_id == task
    assert not reader_backend.list_objects("other-data/audio/")
