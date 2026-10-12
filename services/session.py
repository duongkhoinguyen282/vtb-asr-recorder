"""Reader and Admin session management and browser recovery helper."""

from __future__ import annotations

import hmac
import secrets
import time
from typing import Dict, Optional, Tuple
import streamlit as st


def get_or_create_reader_session() -> Tuple[str, Optional[str], Optional[str], Optional[str]]:
    """Retrieve or initialize anonymous reader session token and state.

    Uses st.query_params for robust browser recovery across page refreshes (F5).
    Returns (session_id, current_task_id, gender, accent).
    """
    # 1. Read existing from query params if present
    query_session = st.query_params.get("s")
    query_task = st.query_params.get("task")
    query_gender = st.query_params.get("g")
    query_accent = st.query_params.get("a")

    # 2. Check session_state
    if "reader_session_id" not in st.session_state:
        if query_session and query_session.strip():
            st.session_state.reader_session_id = query_session.strip()
        else:
            # Generate new cryptographically secure opaque token
            st.session_state.reader_session_id = secrets.token_urlsafe(16)

    session_id = st.session_state.reader_session_id

    # Sync back to query params so F5 rehydrates the session
    st.query_params["s"] = session_id

    # Restore task / gender if in session_state, else fallback to query params
    current_task = st.session_state.get("current_task_id") or query_task
    gender = st.session_state.get("reader_gender") or query_gender
    accent = st.session_state.get("reader_accent") or query_accent

    return session_id, current_task, gender, accent


def set_reader_state(
    session_id: str,
    task_id: Optional[str] = None,
    gender: Optional[str] = None,
    accent: Optional[str] = None,
    dataset_name: Optional[str] = None,
) -> None:
    """Persist reader profile and task pointers in session state and query params."""
    st.session_state.reader_session_id = session_id
    st.query_params["s"] = session_id
    if dataset_name:
        st.query_params["d"] = dataset_name

    if task_id:
        st.session_state.current_task_id = task_id
        st.query_params["task"] = task_id
    else:
        if "task" in st.query_params:
            del st.query_params["task"]
        st.session_state.pop("current_task_id", None)

    if gender:
        st.session_state.reader_gender = gender
        st.query_params["g"] = gender

    if accent:
        st.session_state.reader_accent = accent
        st.query_params["a"] = accent


def clear_reader_task() -> None:
    """Clear active task pointer after submission or skip."""
    st.session_state.pop("current_task_id", None)
    if "task" in st.query_params:
        del st.query_params["task"]
    if "d" in st.query_params:
        del st.query_params["d"]


# --- Admin Authentication & Throttling ---

ADMIN_THROTTLE_MAX_ATTEMPTS = 5
ADMIN_THROTTLE_LOCKOUT_SECONDS = 60


def verify_admin_password(input_password: str, correct_password: str) -> Tuple[bool, str]:
    """Verify admin password using constant-time comparison with brute-force throttling."""
    now = time.time()
    attempts = st.session_state.get("admin_failed_attempts", 0)
    last_failed_time = st.session_state.get("admin_last_failed_time", 0.0)

    # Check lockout
    if attempts >= ADMIN_THROTTLE_MAX_ATTEMPTS:
        elapsed = now - last_failed_time
        if elapsed < ADMIN_THROTTLE_LOCKOUT_SECONDS:
            remaining = int(ADMIN_THROTTLE_LOCKOUT_SECONDS - elapsed)
            return False, f"Quá nhiều lần thử sai. Vui lòng đợi {remaining} giây trước khi thử lại."
        else:
            # Reset after lockout duration passed
            st.session_state.admin_failed_attempts = 0

    if not correct_password:
        return False, "Hệ thống chưa cấu hình mật khẩu quản trị (ADMIN_PASSWORD)."

    # Constant-time comparison
    match = hmac.compare_digest(input_password.strip().encode("utf-8"), correct_password.strip().encode("utf-8"))
    if match:
        st.session_state.admin_authenticated = True
        st.session_state.admin_failed_attempts = 0
        if "admin_reviewer_id" not in st.session_state:
            st.session_state.admin_reviewer_id = f"admin-{secrets.token_hex(4)}"
        return True, "Đăng nhập thành công."
    else:
        st.session_state.admin_failed_attempts = attempts + 1
        st.session_state.admin_last_failed_time = now
        remaining_attempts = ADMIN_THROTTLE_MAX_ATTEMPTS - (attempts + 1)
        if remaining_attempts <= 0:
            return False, f"Mật khẩu không đúng. Tài khoản tạm khóa trong {ADMIN_THROTTLE_LOCKOUT_SECONDS} giây."
        return False, f"Mật khẩu không đúng. Còn {remaining_attempts} lần thử."


def is_admin_authenticated() -> bool:
    """Check if current session is authenticated as admin."""
    return bool(st.session_state.get("admin_authenticated", False))


def admin_logout() -> None:
    """Log out admin session."""
    st.session_state.admin_authenticated = False
    st.session_state.pop("admin_reviewer_id", None)
    st.session_state.pop("admin_active_review_lease", None)

