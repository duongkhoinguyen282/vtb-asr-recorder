"""Reader view: Simple, mobile-friendly Vietnamese recording interface."""

from __future__ import annotations

import logging
import uuid
from typing import Optional
import streamlit as st

from schemas import VoiceEligibility, VoiceGender
from services.dataset_registry import ensure_initialized, get_registry
from services.manifest import load_manifest_from_storage
from services.session import (
    clear_reader_task,
    get_or_create_reader_session,
    set_reader_state,
)
from services.storage import get_storage
from services.tasks import (
    allocate_task,
    get_dataset_summary,
    get_task_state,
    release_lease,
    renew_lease,
    submit_recording,
)

logger = logging.getLogger(__name__)


def render_reader_page() -> None:
    """Render the public Vietnamese recording screen."""
    storage = get_storage()

    # Ensure dataset is initialized
    try:
        registry = ensure_initialized(storage)
    except Exception as exc:
        st.error(f"Lỗi khởi tạo hệ thống lưu trữ: {exc}")
        return

    attempt = st.session_state.get("reader_attempt")
    recovery_dataset = st.query_params.get("d")
    active_dataset = attempt["dataset"] if attempt else (
        recovery_dataset if recovery_dataset in registry.datasets else registry.active_dataset
    )

    # Load active dataset manifest
    try:
        manifest = load_manifest_from_storage(storage, active_dataset)
    except Exception as exc:
        st.error(f"Không thể tải kịch bản cho tập dữ liệu '{active_dataset}': {exc}")
        return

    # Header & Purpose
    st.markdown(
        """
        <div style="text-align: center; margin-bottom: 24px;">
            <h1 style="color: #0054A6; margin-bottom: 8px;">Thu Âm Dữ Liệu Giọng Nói Tiếng Việt</h1>
            <p style="color: #555; font-size: 15px;">
                Dự án thu thập giọng đọc tự nhiên phục vụ huấn luyện và đánh giá mô hình nhận dạng tiếng nói (ASR).
            </p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # Consent and privacy disclaimer
    with st.expander("Thông tin bảo mật & Cam kết dữ liệu", expanded=False):
        st.markdown(
            """
            - **Mục đích sử dụng**: Dữ liệu âm thanh được lưu trữ riêng tư trong Hugging Face Storage Bucket bảo mật và chỉ phục vụ mục đích nghiên cứu, đánh giá mô hình nhận dạng tiếng nói tiếng Việt.
            - **Tính ẩn danh**: Bạn tham gia hoàn toàn ẩn danh, hệ thống **không** yêu cầu và không lưu trữ tên thật, email, số điện thoại hay thông tin định danh cá nhân.
            - **Tự nguyện**: Bạn có thể thu bao nhiêu câu tùy ý ("Ghi được bao nhiêu thì ghi"), và có thể dừng lại bất cứ lúc nào.
            """
        )

    # Retrieve or restore reader session (reloads survive via query params)
    session_id, current_task_id, saved_gender, saved_accent = get_or_create_reader_session()

    # Profile selection: Gender & Accent
    st.subheader("1. Thông tin người đọc")
    col_g, col_a = st.columns([1, 1])

    gender_options = ["Chưa chọn", "Nam", "Nữ"]
    gender_idx = 0
    if saved_gender == "male":
        gender_idx = 1
    elif saved_gender == "female":
        gender_idx = 2

    with col_g:
        selected_gender_raw = st.selectbox(
            "Giới tính giọng nói *",
            gender_options,
            index=gender_idx,
            disabled=attempt is not None,
            help="Cần thiết để phân bổ câu đọc phù hợp với đại từ xưng hô trong kịch bản.",
        )

    accent_options = ["Bắc", "Trung", "Nam", "Khác"]
    accent_idx = 0
    if saved_accent in ("bac", "trung", "nam", "khac"):
        accent_idx = ["bac", "trung", "nam", "khac"].index(saved_accent)

    with col_a:
        selected_accent_raw = st.selectbox(
            "Chất giọng vùng miền (tùy chọn)",
            accent_options,
            index=accent_idx,
            disabled=attempt is not None,
            help="Giúp hệ thống đa dạng hóa dữ liệu vùng miền.",
        )

    if selected_gender_raw == "Chưa chọn":
        st.info("👋 Vui lòng chọn giới tính giọng nói ở trên để nhận câu đọc phù hợp.")
        # Summary progress
        _render_progress_bar(storage, active_dataset, manifest)
        return

    gender_val = "male" if selected_gender_raw == "Nam" else "female"
    accent_map = {"Bắc": "bac", "Trung": "trung", "Nam": "nam", "Khác": "khac"}
    accent_val = accent_map.get(selected_accent_raw, "bac")

    # Update reader profile
    set_reader_state(session_id, current_task_id, gender_val, accent_val)

    # Check if active dataset has tasks
    st.markdown("---")
    st.subheader("2. Câu đọc của bạn")

    # Bind the live recording to its original task/lease across every rerun.
    # Allocation must never run ahead of a pending submit or replace its widget.
    if attempt is None:
        manifest_item, task_state, etag = allocate_task(
            storage=storage,
            dataset_name=active_dataset,
            manifest=manifest,
            session_id=session_id,
            speaker_gender=gender_val,
            accent=accent_val,
            known_task_id=current_task_id,
        )
        if manifest_item and task_state and task_state.recording_lease:
            attempt = dict(dataset=active_dataset, item=manifest_item, state=task_state,
                           lease_id=task_state.recording_lease.lease_id,
                           token=str(uuid.uuid4()), widget_id=str(uuid.uuid4()),
                           audio=None, committed=False)
            st.session_state.reader_attempt = attempt
    else:
        manifest_item, task_state = attempt["item"], attempt["state"]

    if not manifest_item or not task_state or not task_state.recording_lease:
        st.warning(
            "Hiện tại đã hết câu phù hợp với nhóm giọng này trong tập dữ liệu đang thu, hoặc tất cả các câu đang được người khác giữ. Xin cảm ơn sự đóng góp của bạn!"
        )
        _render_progress_bar(storage, active_dataset, manifest)
        return

    # Update session with active task ID
    set_reader_state(session_id, manifest_item.id, gender_val, accent_val, active_dataset)
    lease_id = attempt["lease_id"]

    if attempt["committed"]:
        st.success("🎉 Bản ghi đã được lưu thành công.")
        if st.button("Tiếp tục câu tiếp theo ➡️"):
            _clear_attempt()
            st.rerun()
        _render_progress_bar(storage, active_dataset, manifest)
        return

    _heartbeat(storage, active_dataset, manifest_item.id, lease_id)

    # Render Task Card
    st.markdown(
        f"""
        <div style="background: #F4F8FC; border-left: 6px solid #0054A6; border-radius: 8px; padding: 18px; margin-bottom: 20px;">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px;">
                <span style="font-weight: bold; color: #0054A6; font-size: 14px;">Mã câu: {manifest_item.id}</span>
                <span style="background: #E0E8F5; color: #103B75; font-size: 12px; padding: 4px 10px; border-radius: 12px;">
                    {manifest_item.domain} · {manifest_item.scenario}
                </span>
            </div>
            <div style="font-size: 21px; line-height: 1.6; color: #1A1A1A; font-weight: 500; margin-bottom: 16px;">
                {manifest_item.reading_script}
            </div>
            <div style="background: #FFF7E6; border: 1px solid #FFE58F; border-radius: 6px; padding: 10px 14px;">
                <div style="font-weight: 600; font-size: 13px; color: #D46B08; margin-bottom: 4px;">
                    ⚠️ Hướng dẫn thể hiện — không đọc phần này:
                </div>
                <div style="font-size: 14px; color: #595959; font-style: italic;">
                    {manifest_item.delivery_instruction}
                </div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # Audio Recording Widget
    st.subheader("3. Thu âm và kiểm tra")
    st.markdown(
        "<p style='font-size: 14px; color: #666;'>Nhấn vào nút micro dưới đây để bắt đầu đọc. Đọc tự nhiên theo hướng dẫn, sau đó nghe lại trước khi gửi.</p>",
        unsafe_allow_html=True,
    )

    # Widget key tied to task ID and submission attempt counter to clear audio on advance
    audio_widget_key = f"audio_rec_{active_dataset}_{manifest_item.id}_{lease_id}_{attempt['widget_id']}"

    audio_file = st.audio_input("Ghi âm giọng đọc (16kHz)", key=audio_widget_key)
    if audio_file is not None:
        captured_audio = audio_file.getvalue()
        if attempt["audio"] is not None and attempt["audio"] != captured_audio:
            attempt["token"] = str(uuid.uuid4())
        attempt["audio"] = captured_audio
    if attempt["audio"]:
        st.audio(attempt["audio"], format="audio/wav")
        st.download_button("Tải bản thu để giữ lại", attempt["audio"],
                           file_name=f"{manifest_item.id}.wav", mime="audio/wav")

    # Action buttons
    col_submit, col_skip = st.columns([2, 1])

    with col_submit:
        submit_btn = st.button("📤 Gửi bản ghi", type="primary", use_container_width=True)

    with col_skip:
        skip_btn = st.button("⏭️ Bỏ qua câu này", use_container_width=True)

    if st.button("🔄 Thu lại", disabled=not bool(attempt["audio"])):
        attempt["audio"] = None
        attempt["token"] = str(uuid.uuid4())
        attempt["widget_id"] = str(uuid.uuid4())
        st.rerun()

    if skip_btn:
        try:
            released = release_lease(storage, active_dataset, manifest_item.id, lease_id)
            current, _ = get_task_state(storage, active_dataset, manifest_item.id)
            still_owned = bool(current and current.recording_lease
                               and current.recording_lease.lease_id == lease_id)
            if released or not still_owned:
                _clear_attempt()
                st.rerun()
            st.error("Chưa thể bỏ qua câu. Vui lòng thử lại.")
        except Exception:
            st.error("Không thể liên hệ kho lưu trữ. Bản thu vẫn được giữ để thử lại.")

    if submit_btn:
        if attempt.get("lease_error"):
            st.error(attempt["lease_error"])
        elif not attempt["audio"]:
            st.warning("Bạn chưa thu âm. Vui lòng nhấn biểu tượng micro để ghi âm trước khi gửi.")
        else:
            audio_bytes = attempt["audio"]
            with st.spinner("Đang kiểm tra và tải lên kho lưu trữ bảo mật..."):
                try:
                    success, msg, _ = submit_recording(
                        storage=storage,
                        dataset_name=active_dataset,
                        task_id=manifest_item.id,
                        lease_id=lease_id,
                        session_id=session_id,
                        audio_bytes=audio_bytes,
                        speaker_gender=task_state.recording_lease.speaker_gender,
                        accent=task_state.recording_lease.accent,
                        idempotency_token=attempt["token"],
                    )
                except Exception:
                    logger.exception("Recording submission could not be confirmed")
                    success, msg = False, "Chưa xác nhận được bản ghi đã lưu. Bản thu vẫn được giữ; vui lòng thử gửi lại."

            if success:
                st.success(f"🎉 {msg}")
                attempt["committed"] = True
                st.rerun()
            else:
                st.error(f"❌ {msg}")
                st.info("Bản thu âm của bạn vẫn được giữ lại bên trên. Bạn có thể nhấn 'Gửi bản ghi' lại.")

    # Progress footer
    _render_progress_bar(storage, active_dataset, manifest)


def _clear_attempt() -> None:
    st.session_state.pop("reader_attempt", None)
    clear_reader_task()


@st.fragment(run_every="60s")
def _heartbeat(storage, dataset: str, task_id: str, lease_id: str) -> None:
    """Renew without recreating the recorder; never switch tasks on lease loss."""
    attempt = st.session_state.get("reader_attempt")
    if not attempt or attempt["committed"]:
        return
    try:
        ok, _, _ = renew_lease(storage, dataset, task_id, lease_id)
        current, _ = get_task_state(storage, dataset, task_id)
        # A previous submit may have committed despite a lost network response.
        own_commit = bool(current and current.current_audio
                          and current.current_audio.recording_id == attempt["token"]
                          and current.current_audio.speaker_id == st.session_state.reader_session_id)
        attempt["lease_error"] = None if ok or own_commit else (
            "Câu này không còn được giữ cho bạn hoặc đã hết hạn. "
            "Hãy tải bản thu để giữ lại, rồi chọn Bỏ qua câu này để nhận câu khác."
        )
    except Exception:
        attempt["lease_error"] = "Không thể kiểm tra thời hạn giữ câu. Bản thu vẫn được giữ; vui lòng thử lại."
    if attempt.get("lease_error"):
        st.error(attempt["lease_error"])


def _render_progress_bar(storage, active_dataset: str, manifest) -> None:
    """Render subtle aggregate progress bar at bottom."""
    st.markdown("---")
    summary = get_dataset_summary(storage, active_dataset, manifest)
    total = summary["total_tasks"]
    recorded = summary["recorded"]
    pct = (recorded / total * 100) if total > 0 else 0

    st.markdown(
        f"""
        <div style="font-size: 13px; color: #777; margin-bottom: 6px; display: flex; justify-content: space-between;">
            <span>Tiến độ tập dữ liệu hiện tại (<b>{active_dataset}</b>):</span>
            <span><b>{recorded}</b> / {total} câu ({pct:.1f}%)</span>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.progress(min(1.0, recorded / max(1, total)))

