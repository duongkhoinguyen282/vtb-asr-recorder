"""Admin view: Dashboard and Review interface with password protection."""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional
import streamlit as st

from schemas import ReviewStatus, TaskStatus
from services.dataset_registry import (
    create_dataset,
    ensure_initialized,
    get_registry,
    set_active_dataset,
)
from services.manifest import load_manifest_from_storage
from services.review import (
    approve_recording,
    claim_review_lease,
    reject_recording,
    release_review_lease,
)
from services.session import (
    admin_logout,
    is_admin_authenticated,
    verify_admin_password,
)
from services.storage import StorageNotFoundError, get_storage
from services.tasks import (
    get_dataset_summary,
    get_task_state,
    load_all_task_states,
)

logger = logging.getLogger(__name__)


def _get_admin_password() -> str:
    """Retrieve configured admin password from secrets or environment."""
    try:
        if hasattr(st, "secrets") and "ADMIN_PASSWORD" in st.secrets:
            return str(st.secrets["ADMIN_PASSWORD"])
    except Exception:
        pass
    return os.environ.get("ADMIN_PASSWORD", "ilovevtb")


def render_admin_page() -> None:
    """Render the password-protected admin page."""
    storage = get_storage()

    # Ensure system is initialized
    try:
        registry = ensure_initialized(storage)
    except Exception as exc:
        st.error(f"Lỗi khởi tạo hệ thống lưu trữ: {exc}")
        return

    # Check authentication
    if not is_admin_authenticated():
        _render_login_screen()
        return

    # Admin Header & Navigation
    _render_admin_header(storage)


def _render_login_screen() -> None:
    """Render shared password login form with rate limiting."""
    st.markdown(
        """
        <div style="text-align: center; margin-top: 40px; margin-bottom: 24px;">
            <h2 style="color: #0054A6;">Khu Vực Quản Trị Hệ Thống ASR</h2>
            <p style="color: #666;">Vui lòng nhập mật khẩu quản trị viên để tiếp tục.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        with st.form("admin_login_form"):
            password_input = st.text_input("Mật khẩu quản trị", type="password")
            reviewer_name_input = st.text_input("Tên/Biệt danh của bạn (tùy chọn)", placeholder="VD: Reviewer 1")
            submit_login = st.form_submit_button("Đăng nhập", type="primary", use_container_width=True)

            if submit_login:
                correct_pw = _get_admin_password()
                ok, msg = verify_admin_password(password_input, correct_pw)
                if ok:
                    if reviewer_name_input.strip():
                        st.session_state.admin_reviewer_name = reviewer_name_input.strip()
                    st.success(msg)
                    st.rerun()
                else:
                    st.error(msg)


def _render_admin_header(storage) -> None:
    """Render admin top bar, dataset selection, [+] Add dataset, and tabs."""
    registry, _ = get_registry(storage)
    dataset_keys = list(registry.datasets.keys())
    active_dataset = registry.active_dataset

    # Top control bar
    col_title, col_user, col_logout = st.columns([3, 2, 1])
    with col_title:
        st.title("Bảng Quản Trị (Admin)")
    with col_user:
        reviewer_id = st.session_state.get("admin_reviewer_id", "admin")
        reviewer_name = st.session_state.get("admin_reviewer_name", reviewer_id)
        st.caption(f"👤 Quản trị viên: **{reviewer_name}**")
    with col_logout:
        if st.button("Đăng xuất", use_container_width=True):
            admin_logout()
            st.rerun()

    st.markdown("---")

    # Dataset selection and [+] button
    col_sel, col_act, col_add = st.columns([3, 2, 1.5])

    # Store selected dataset in session state so it persists across tab switches
    if "admin_selected_dataset" not in st.session_state or st.session_state.admin_selected_dataset not in dataset_keys:
        st.session_state.admin_selected_dataset = active_dataset

    with col_sel:
        def _format_ds_label(k: str) -> str:
            cfg = registry.datasets.get(k)
            name = cfg.display_name if cfg else k
            tag = " ⭐ (Đang thu)" if k == active_dataset else ""
            return f"{name} [{k}]{tag}"

        current_idx = dataset_keys.index(st.session_state.admin_selected_dataset) if st.session_state.admin_selected_dataset in dataset_keys else 0
        selected_ds = st.selectbox(
            "Chọn tập dữ liệu để quản lý / kiểm tra:",
            dataset_keys,
            index=current_idx,
            format_func=_format_ds_label,
        )
        st.session_state.admin_selected_dataset = selected_ds

    with col_act:
        st.write("")
        st.write("")
        if selected_ds != active_dataset:
            if st.button("⭐ Đặt làm dataset đang thu", type="secondary", use_container_width=True):
                try:
                    set_active_dataset(storage, selected_ds)
                    st.success(f"Đã kích hoạt '{selected_ds}' cho cộng tác viên đọc.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Lỗi kích hoạt dataset: {exc}")
        else:
            st.info("Đang là dataset thu âm chính thức")

    with col_add:
        st.write("")
        st.write("")
        show_add_modal = st.button("➕ Thêm dataset", type="primary", use_container_width=True)

    if show_add_modal or st.session_state.get("show_add_dataset_modal", False):
        st.session_state.show_add_dataset_modal = True
        _render_add_dataset_dialog(storage)

    # Load manifest for selected dataset
    try:
        manifest = load_manifest_from_storage(storage, selected_ds)
    except Exception as exc:
        st.error(f"Không thể đọc manifest của dataset '{selected_ds}': {exc}")
        return

    # Two Main Tabs: Dashboard and Review
    tab_dash, tab_rev = st.tabs(["📊 Tổng quan (Dashboard)", "🎧 Kiểm duyệt (Review)"])

    with tab_dash:
        _render_dashboard_tab(storage, selected_ds, manifest)

    with tab_rev:
        _render_review_tab(storage, selected_ds, manifest)


def _render_add_dataset_dialog(storage) -> None:
    """Render dialog to create a new dataset namespace."""
    with st.expander("Thêm Tập Dữ Liệu Mới (Tạo Namespace)", expanded=True):
        st.markdown(
            """
            Tạo một tập kịch bản mới trong bucket. Hệ thống sẽ **không** tự động chuyển dataset này thành dataset đang thu,
            cho đến khi bạn nhấn nút 'Đặt làm dataset đang thu'.
            """
        )
        with st.form("add_dataset_form"):
            new_slug = st.text_input("Mã định danh (slug) *", placeholder="VD: numeric-data-v2", help="Chỉ dùng chữ thường, số và dấu gạch nối.")
            new_display = st.text_input("Tên hiển thị *", placeholder="VD: Tập số đợt 2")
            new_desc = st.text_area("Mô tả (tùy chọn)", placeholder="Mô tả mục đích hoặc kịch bản...")
            uploaded_jsonl = st.file_uploader("Tải lên tệp manifest (.jsonl) *", type=["jsonl", "txt"])

            col_submit, col_cancel = st.columns([1, 1])
            with col_submit:
                create_btn = st.form_submit_button("Tạo Dataset", type="primary", use_container_width=True)
            with col_cancel:
                cancel_btn = st.form_submit_button("Hủy", use_container_width=True)

            if cancel_btn:
                st.session_state.show_add_dataset_modal = False
                st.rerun()

            if create_btn:
                if not uploaded_jsonl:
                    st.error("Vui lòng tải lên tệp manifest .jsonl")
                else:
                    content_bytes = uploaded_jsonl.read()
                    with st.spinner("Đang xác thực và tải lên dataset..."):
                        ok, msg, errors = create_dataset(
                            storage=storage,
                            name=new_slug.strip(),
                            display_name=new_display.strip(),
                            manifest_bytes=content_bytes,
                            description=new_desc.strip(),
                        )
                    if ok:
                        st.success(f"🎉 {msg}")
                        st.session_state.show_add_dataset_modal = False
                        st.session_state.admin_selected_dataset = new_slug.strip()
                        st.rerun()
                    else:
                        st.error(f"❌ {msg}")
                        if errors:
                            with st.expander(f"Xem chi tiết {len(errors)} lỗi cần khắc phục", expanded=True):
                                for err in errors[:20]:
                                    st.write(f"- {err}")
                                if len(errors) > 20:
                                    st.caption(f"...và {len(errors) - 20} lỗi khác.")


def _render_dashboard_tab(storage, dataset_name: str, manifest) -> None:
    """Render Dashboard tab metrics and list of recorded clips."""
    summary = get_dataset_summary(storage, dataset_name, manifest)

    total = summary["total_tasks"]
    recorded = summary["recorded"]
    available = summary["available"]
    reserved = summary["reserved"]
    unchecked = summary["unchecked"]
    approved = summary["approved"]
    approved_edited = summary["approved_edited"]

    # Top metrics row
    st.subheader("Số lượng tiến độ tổng thể")
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Tổng số câu", total)
    m2.metric("Đã thu", f"{recorded} ({recorded/max(1, total)*100:.1f}%)")
    m3.metric("Chưa thu", f"{available + reserved} (Đang giữ: {reserved})")
    m4.metric("Chưa kiểm tra", unchecked)
    m5.metric("Đã kiểm tra", f"{approved + approved_edited} (Sửa: {approved_edited})")

    st.markdown("---")

    # Demographic & scenario breakdowns
    col_left, col_right = st.columns(2)
    with col_left:
        st.subheader("Phân bổ giọng đọc đã thu")
        st.write(f"- **Giọng Nam**: {summary['recorded_males']} câu")
        st.write(f"- **Giọng Nữ**: {summary['recorded_females']} câu")

    with col_right:
        st.subheader("Phân bổ theo kịch bản (Scenario)")
        if summary["scenarios_recorded"]:
            for sc, count in sorted(summary["scenarios_recorded"].items(), key=lambda x: -x[1]):
                st.write(f"- **{sc}**: {count} câu")
        else:
            st.caption("Chưa có dữ liệu bản ghi.")

    st.markdown("---")

    # Search & Audio List
    st.subheader("Danh sách các bản ghi hiện có")
    search_query = st.text_input("Tìm kiếm theo Task ID:", placeholder="VD: HNUM-0001", key="dash_search")

    states = load_all_task_states(storage, dataset_name)
    manifest_by_id = {item.id: item for item in manifest}

    recorded_rows = []
    for task_id, (st_obj, _) in states.items():
        if st_obj.status == TaskStatus.RECORDED and st_obj.current_audio:
            if not search_query.strip() or search_query.strip().lower() in task_id.lower():
                item = manifest_by_id.get(task_id)
                recorded_rows.append((task_id, item, st_obj))

    if not recorded_rows:
        st.info("Chưa có bản ghi nào phù hợp với bộ lọc.")
    else:
        st.caption(f"Tìm thấy {len(recorded_rows)} bản ghi:")
        for task_id, item, st_obj in recorded_rows[:30]:
            audio = st_obj.current_audio
            with st.expander(f"🎧 {task_id} — Giọng: {audio.voice_gender} ({audio.duration_seconds}s) — Trạng thái: {st_obj.review.status.value}"):
                if item:
                    st.markdown(f"**Kịch bản**: {item.reading_script}")
                    st.caption(f"Hướng dẫn: {item.delivery_instruction}")

                # Secure server playback: fetch bytes via storage
                try:
                    audio_bytes, _ = storage.get_object(audio.object_key)
                    st.audio(audio_bytes, format="audio/wav")
                except Exception as exc:
                    st.error(f"Không thể tải tệp âm thanh: {exc}")

                st.caption(
                    f"Mã bản ghi: `{audio.recording_id}` · Thời gian tải: {audio.uploaded_at}"
                )


def _render_review_tab(storage, dataset_name: str, manifest) -> None:
    """Render QA Review workflow with exclusive leases and 3 decisions."""
    st.subheader("Kiểm duyệt bản ghi (QA)")
    st.caption("Nghe lại các bản ghi đã thu, kiểm tra chất lượng và xác nhận hoặc mở lại cho thu lại nếu không đạt.")

    states = load_all_task_states(storage, dataset_name)
    manifest_by_id = {item.id: item for item in manifest}

    # Filter controls
    col_f1, col_f2, col_f3 = st.columns([2, 1, 1])
    with col_f1:
        search_id = st.text_input("Tìm nhanh Task ID:", placeholder="HNUM-...", key="rev_search")
    with col_f2:
        filter_status = st.selectbox(
            "Lọc trạng thái duyệt:",
            ["Tất cả", "Chưa kiểm tra", "Đã duyệt (chuẩn)", "Đã duyệt (sửa text)", "Bị từ chối"],
        )
    with col_f3:
        filter_gender = st.selectbox("Lọc giới tính:", ["Tất cả", "Nam", "Nữ"])

    # Filter recorded items
    candidates = []
    for task_id, (st_obj, _) in states.items():
        if st_obj.status == TaskStatus.RECORDED and st_obj.current_audio:
            if search_id.strip() and search_id.strip().lower() not in task_id.lower():
                continue
            if filter_gender == "Nam" and st_obj.current_audio.voice_gender.lower() != "male":
                continue
            if filter_gender == "Nữ" and st_obj.current_audio.voice_gender.lower() != "female":
                continue

            r_status = st_obj.review.status
            if filter_status == "Chưa kiểm tra" and r_status != ReviewStatus.UNCHECKED:
                continue
            if filter_status == "Đã duyệt (chuẩn)" and r_status != ReviewStatus.APPROVED:
                continue
            if filter_status == "Đã duyệt (sửa text)" and r_status != ReviewStatus.APPROVED_EDITED:
                continue
            if filter_status == "Bị từ chối" and r_status != ReviewStatus.REJECTED:
                continue

            candidates.append((task_id, manifest_by_id.get(task_id), st_obj))

    if not candidates:
        st.info("Không có bản ghi nào phù hợp với bộ lọc kiểm duyệt.")
        return

    # Task selector or next button
    task_ids = [c[0] for c in candidates]
    col_sel, col_next = st.columns([3, 1])
    with col_sel:
        selected_task_id = st.selectbox(
            "Chọn câu để kiểm tra:",
            task_ids,
            format_func=lambda tid: f"{tid} - [{states[tid][0].review.status.value}]",
            key="rev_task_selector",
        )
    with col_next:
        st.write("")
        st.write("")
        if st.button("Câu tiếp theo ➡️", use_container_width=True):
            # Advance to next unchecked candidate
            unchecked_cands = [c[0] for c in candidates if c[2].review.status == ReviewStatus.UNCHECKED and c[0] != selected_task_id]
            if unchecked_cands:
                st.session_state.rev_task_selector = unchecked_cands[0]
                st.rerun()

    # Load selected task details
    target_state, _ = get_task_state(storage, dataset_name, selected_task_id)
    target_item = manifest_by_id.get(selected_task_id)

    if not target_state or not target_state.current_audio:
        st.error("Không tìm thấy dữ liệu bản ghi cho câu này.")
        return

    audio_meta = target_state.current_audio

    st.markdown("---")

    # Render Task Review Card
    st.markdown(
        f"""
        <div style="background: #F9FAFB; border: 1px solid #E5E7EB; border-radius: 8px; padding: 16px; margin-bottom: 16px;">
            <div style="display: flex; justify-content: space-between; margin-bottom: 8px;">
                <span style="font-weight: bold; color: #0054A6; font-size: 16px;">Mã câu: {selected_task_id}</span>
                <span style="font-size: 13px; color: #666;">
                    Giọng: <b>{audio_meta.voice_gender}</b> · Vùng miền: <b>{audio_meta.accent or 'N/A'}</b> · Thời lượng: <b>{audio_meta.duration_seconds}s</b>
                </span>
            </div>
            <div style="font-size: 18px; line-height: 1.5; color: #111; margin-bottom: 12px;">
                <b>Kịch bản gốc</b>: {target_item.reading_script if target_item else 'N/A'}
            </div>
            <div style="font-size: 14px; color: #555; background: #FFFBEB; border-left: 4px solid #F59E0B; padding: 8px 12px; margin-bottom: 12px;">
                <b>Hướng dẫn thể hiện</b>: {target_item.delivery_instruction if target_item else 'N/A'}
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # Audio Playback
    st.write("🎵 **Bản ghi âm:**")
    try:
        audio_bytes, _ = storage.get_object(audio_meta.object_key)
        st.audio(audio_bytes, format="audio/wav")
    except Exception as exc:
        st.error(f"Không thể đọc file audio từ storage: {exc}")
        return

    # Current Review Status
    st.caption(f"Trạng thái hiện tại: **{target_state.review.status.value}**")
    if target_state.review.verified_transcript:
        st.info(f"Văn bản đã sửa: {target_state.review.verified_transcript}")

    # Exclusive Review Lease Management
    reviewer_id = st.session_state.get("admin_reviewer_id", "admin")
    reviewer_name = st.session_state.get("admin_reviewer_name", reviewer_id)

    # Review Action Forms (Decision 1, 2, 3)
    st.markdown("### Quyết định kiểm duyệt")

    col_d1, col_d2, col_d3 = st.columns(3)

    # Decision 1: Đạt (Approve)
    with col_d1:
        st.markdown("#### 1. Đạt (Chuẩn)")
        st.caption("Bản ghi đạt chuẩn, nội dung đọc khớp với kịch bản.")
        if st.button("✅ Duyệt Đạt", type="primary", use_container_width=True, key=f"btn_app_{selected_task_id}"):
            # Claim review lease and commit
            lease_ok, lease_msg, lease_obj = claim_review_lease(
                storage, dataset_name, selected_task_id, reviewer_id, reviewer_name
            )
            if not lease_ok or not lease_obj:
                st.error(lease_msg)
            else:
                ok, msg = approve_recording(
                    storage=storage,
                    dataset_name=dataset_name,
                    task_id=selected_task_id,
                    review_lease_id=lease_obj.lease_id,
                    reviewer_id=reviewer_id,
                    reviewer_name=reviewer_name,
                    expected_recording_id=audio_meta.recording_id,
                )
                if ok:
                    st.success(msg)
                    st.rerun()
                else:
                    st.error(msg)

    # Decision 2: Đạt, sửa text (Approve with transcript edit)
    with col_d2:
        st.markdown("#### 2. Đạt (Sửa text)")
        st.caption("Âm thanh đạt, nhưng có từ vấp/lặp khác kịch bản gốc.")
        with st.expander("Sửa nội dung văn bản thực tế", expanded=False):
            initial_text = target_state.review.verified_transcript or (target_item.reading_script if target_item else "")
            edited_text = st.text_area(
                "Nội dung người đọc THỰC TẾ phát âm:",
                value=initial_text,
                key=f"edit_text_{selected_task_id}",
            )
            if st.button("💾 Duyệt & Lưu văn bản sửa", key=f"btn_edit_{selected_task_id}", use_container_width=True):
                if not edited_text.strip():
                    st.error("Văn bản sửa không được để trống.")
                else:
                    lease_ok, lease_msg, lease_obj = claim_review_lease(
                        storage, dataset_name, selected_task_id, reviewer_id, reviewer_name
                    )
                    if not lease_ok or not lease_obj:
                        st.error(lease_msg)
                    else:
                        ok, msg = approve_recording(
                            storage=storage,
                            dataset_name=dataset_name,
                            task_id=selected_task_id,
                            review_lease_id=lease_obj.lease_id,
                            reviewer_id=reviewer_id,
                            reviewer_name=reviewer_name,
                            verified_transcript=edited_text.strip(),
                            expected_recording_id=audio_meta.recording_id,
                        )
                        if ok:
                            st.success(msg)
                            st.rerun()
                        else:
                            st.error(msg)

    # Decision 3: Không đạt / Thu lại (Reject)
    with col_d3:
        st.markdown("#### 3. Không đạt (Thu lại)")
        st.caption("Âm thanh hỏng/lỗi, mở lại câu cho cộng tác viên đọc lại.")
        with st.expander("Chọn lý do từ chối", expanded=False):
            reasons = []
            if st.checkbox("Ồn / khó nghe", key=f"r1_{selected_task_id}"):
                reasons.append("Ồn / khó nghe")
            if st.checkbox("Âm lượng quá nhỏ", key=f"r2_{selected_task_id}"):
                reasons.append("Âm lượng quá nhỏ")
            if st.checkbox("Bị cắt đầu / cắt đuôi", key=f"r3_{selected_task_id}"):
                reasons.append("Bị cắt đầu / cắt đuôi")
            if st.checkbox("Nuốt chữ / không rõ âm", key=f"r4_{selected_task_id}"):
                reasons.append("Nuốt chữ / không rõ âm")
            if st.checkbox("Đọc thiếu / thừa từ", key=f"r5_{selected_task_id}"):
                reasons.append("Đọc thiếu / thừa từ")
            if st.checkbox("Sai / thiếu chữ số", key=f"r6_{selected_task_id}"):
                reasons.append("Sai / thiếu chữ số")
            if st.checkbox("Ngữ điệu không tự nhiên", key=f"r7_{selected_task_id}"):
                reasons.append("Ngữ điệu không tự nhiên")
            if st.checkbox("Lý do khác", key=f"r8_{selected_task_id}"):
                reasons.append("Khác")

            note_input = st.text_input("Ghi chú thêm (tùy chọn):", key=f"r_note_{selected_task_id}")

            if st.button("❌ Xác nhận yêu cầu Thu lại", type="secondary", use_container_width=True, key=f"btn_rej_{selected_task_id}"):
                lease_ok, lease_msg, lease_obj = claim_review_lease(
                    storage, dataset_name, selected_task_id, reviewer_id, reviewer_name
                )
                if not lease_ok or not lease_obj:
                    st.error(lease_msg)
                else:
                    ok, msg = reject_recording(
                        storage=storage,
                        dataset_name=dataset_name,
                        task_id=selected_task_id,
                        review_lease_id=lease_obj.lease_id,
                        reviewer_id=reviewer_id,
                        reviewer_name=reviewer_name,
                        error_reasons=reasons,
                        note=note_input,
                        expected_recording_id=audio_meta.recording_id,
                    )
                    if ok:
                        st.warning(msg)
                        st.rerun()
                    else:
                        st.error(msg)

