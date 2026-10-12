"""Main application entrypoint for VietinBank Vietnamese ASR recording app.

Defines public reader screen at '/' and hidden password-protected admin route at '/admin'.
"""

from __future__ import annotations

import streamlit as st

from views.admin_view import render_admin_page
from views.reader_view import render_reader_page

st.set_page_config(
    page_title="VietinBank Vietnamese ASR Recording",
    page_icon="🎙️",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# Define routes
reader_page = st.Page(
    render_reader_page,
    title="Thu Âm Giọng Nói",
    icon="🎙️",
    url_path="",
    default=True,
)

admin_page = st.Page(
    render_admin_page,
    title="Bảng Quản Trị",
    icon="⚙️",
    url_path="admin",
)

# Keep navigation hidden from public reader UI while supporting direct /admin URL
router = st.navigation([reader_page, admin_page], position="hidden")
router.run()

