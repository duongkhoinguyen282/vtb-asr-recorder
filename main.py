"""FastAPI Backend for VietinBank Vietnamese ASR Recording & QA Review System."""

from __future__ import annotations

import hmac
import logging
import os
import secrets
import time
from typing import Dict, List, Optional

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from schemas import TaskStatus
from services.dataset_registry import (
    create_dataset,
    ensure_initialized,
    set_active_dataset,
)
from services.manifest import load_manifest_from_storage
from services.review import approve_recording, claim_review_lease, reject_recording
from services.storage import StorageNotFoundError, get_storage
from services.tasks import (
    allocate_task,
    get_dataset_summary,
    load_all_task_states,
    release_lease,
    renew_lease,
    submit_recording,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="VietinBank Vietnamese ASR Recording API")

ADMIN_SESSIONS: Dict[str, Dict[str, str]] = {}
LOGIN_ATTEMPTS: Dict[str, List[float]] = {}
MAX_ATTEMPTS = 5
LOCKOUT_SECONDS = 60


def get_admin_password() -> str:
    return os.environ.get("ADMIN_PASSWORD", "ilovevtb")


def require_admin(x_admin_token: Optional[str] = Header(None)) -> Dict[str, str]:
    if not x_admin_token or x_admin_token not in ADMIN_SESSIONS:
        raise HTTPException(status_code=401, detail="Phiên quản trị không hợp lệ hoặc đã hết hạn.")
    return ADMIN_SESSIONS[x_admin_token]


@app.on_event("startup")
def startup_event():
    storage = get_storage()
    try:
        reg = ensure_initialized(storage)
        load_manifest_from_storage(storage, reg.active_dataset)
        load_all_task_states(storage, reg.active_dataset)
        logger.info("System initialized & warmed up active dataset: %s", reg.active_dataset)
    except Exception as exc:
        logger.error("Startup initialization warning: %s", exc)


@app.get("/api/reader/init")
def reader_init(dataset: Optional[str] = None):
    storage = get_storage()
    reg = ensure_initialized(storage)
    active_ds = dataset if (dataset and dataset in reg.datasets) else reg.active_dataset
    manifest = load_manifest_from_storage(storage, active_ds)
    summary = get_dataset_summary(storage, active_ds, manifest)
    return {
        "active_dataset": active_ds,
        "dataset_info": reg.datasets.get(active_ds),
        "summary": summary,
    }


class AllocateRequest(BaseModel):
    session_id: Optional[str] = None
    gender: str
    accent: str = "bac"
    known_task_id: Optional[str] = None
    dataset: Optional[str] = None


@app.post("/api/reader/allocate")
def reader_allocate(req: AllocateRequest):
    storage = get_storage()
    reg = ensure_initialized(storage)
    active_ds = req.dataset if (req.dataset and req.dataset in reg.datasets) else reg.active_dataset
    manifest = load_manifest_from_storage(storage, active_ds)

    session_id = req.session_id.strip() if (req.session_id and req.session_id.strip()) else secrets.token_urlsafe(16)
    gender = req.gender.lower().strip()
    if gender not in ("male", "female"):
        raise HTTPException(status_code=400, detail="Giới tính phải là 'male' hoặc 'female'.")

    item, state, _ = allocate_task(
        storage=storage,
        dataset_name=active_ds,
        manifest=manifest,
        session_id=session_id,
        speaker_gender=gender,
        accent=req.accent,
        known_task_id=req.known_task_id,
    )
    summary = get_dataset_summary(storage, active_ds, manifest)

    if not item or not state or not state.recording_lease:
        return {
            "session_id": session_id,
            "active_dataset": active_ds,
            "exhausted": True,
            "message": "Hiện tại đã hết câu phù hợp với nhóm giọng này trong tập dữ liệu đang thu, hoặc tất cả các câu đang được người khác giữ. Xin cảm ơn sự đóng góp của bạn!",
            "summary": summary,
        }

    return {
        "session_id": session_id,
        "active_dataset": active_ds,
        "exhausted": False,
        "task": item.model_dump(),
        "lease": state.recording_lease.model_dump(),
        "idempotency_token": str(secrets.token_hex(16)),
        "summary": summary,
    }


class LeaseActionRequest(BaseModel):
    dataset: str
    task_id: str
    lease_id: str


@app.post("/api/reader/heartbeat")
def reader_heartbeat(req: LeaseActionRequest):
    storage = get_storage()
    ok, msg, state = renew_lease(storage, req.dataset, req.task_id, req.lease_id)
    return {
        "ok": ok,
        "message": msg,
        "expires_at": state.recording_lease.expires_at if (state and state.recording_lease) else None,
    }


@app.post("/api/reader/skip")
def reader_skip(req: LeaseActionRequest):
    storage = get_storage()
    released = release_lease(storage, req.dataset, req.task_id, req.lease_id)
    return {"ok": released}


@app.post("/api/reader/submit")
async def reader_submit(
    dataset: str = Form(...),
    task_id: str = Form(...),
    lease_id: str = Form(...),
    session_id: str = Form(...),
    gender: str = Form(...),
    accent: str = Form("bac"),
    idempotency_token: str = Form(...),
    audio: UploadFile = File(...),
):
    storage = get_storage()
    audio_bytes = await audio.read()

    success, msg, _ = submit_recording(
        storage=storage,
        dataset_name=dataset,
        task_id=task_id,
        lease_id=lease_id,
        session_id=session_id,
        audio_bytes=audio_bytes,
        speaker_gender=gender,
        accent=accent,
        idempotency_token=idempotency_token,
    )
    if not success:
        raise HTTPException(status_code=400, detail=msg)

    manifest = load_manifest_from_storage(storage, dataset)
    summary = get_dataset_summary(storage, dataset, manifest)
    return {"ok": True, "message": msg, "summary": summary}


class AdminLoginRequest(BaseModel):
    password: str
    reviewer_name: Optional[str] = None


@app.post("/api/admin/login")
def admin_login(req: AdminLoginRequest, request: Request):
    client_ip = request.client.host if request.client else "unknown"
    now = time.time()

    attempts = [t for t in LOGIN_ATTEMPTS.get(client_ip, []) if now - t < LOCKOUT_SECONDS]
    LOGIN_ATTEMPTS[client_ip] = attempts

    if len(attempts) >= MAX_ATTEMPTS:
        remaining = int(LOCKOUT_SECONDS - (now - attempts[0]))
        raise HTTPException(
            status_code=429,
            detail=f"Quá nhiều lần thử sai. Vui lòng đợi {remaining} giây trước khi thử lại.",
        )

    correct_pw = get_admin_password()
    if not hmac.compare_digest(req.password.strip().encode("utf-8"), correct_pw.strip().encode("utf-8")):
        LOGIN_ATTEMPTS.setdefault(client_ip, []).append(now)
        rem = MAX_ATTEMPTS - len(LOGIN_ATTEMPTS[client_ip])
        raise HTTPException(status_code=401, detail=f"Mật khẩu không đúng. Còn {max(0, rem)} lần thử.")

    LOGIN_ATTEMPTS.pop(client_ip, None)
    token = secrets.token_urlsafe(24)
    reviewer_id = f"admin-{secrets.token_hex(4)}"
    reviewer_name = req.reviewer_name.strip() if (req.reviewer_name and req.reviewer_name.strip()) else reviewer_id

    ADMIN_SESSIONS[token] = {"reviewer_id": reviewer_id, "reviewer_name": reviewer_name}
    return {"token": token, "reviewer_id": reviewer_id, "reviewer_name": reviewer_name}


@app.post("/api/admin/logout")
def admin_logout(x_admin_token: Optional[str] = Header(None)):
    if x_admin_token:
        ADMIN_SESSIONS.pop(x_admin_token, None)
    return {"ok": True}


@app.get("/api/admin/overview")
def admin_overview(dataset: Optional[str] = None, admin: dict = Depends(require_admin)):
    storage = get_storage()
    reg = ensure_initialized(storage)
    selected_ds = dataset if (dataset and dataset in reg.datasets) else reg.active_dataset

    manifest = load_manifest_from_storage(storage, selected_ds)
    summary = get_dataset_summary(storage, selected_ds, manifest)
    states = load_all_task_states(storage, selected_ds)
    manifest_by_id = {item.id: item for item in manifest}

    recorded_items = []
    for task_id, (st_obj, _) in sorted(states.items()):
        if st_obj.status == TaskStatus.RECORDED and st_obj.current_audio:
            m_item = manifest_by_id.get(task_id)
            recorded_items.append({
                "task_id": task_id,
                "reading_script": m_item.reading_script if m_item else "",
                "delivery_instruction": m_item.delivery_instruction if m_item else "",
                "domain": m_item.domain if m_item else "",
                "scenario": m_item.scenario if m_item else "",
                "audio": st_obj.current_audio.model_dump(),
                "review": st_obj.review.model_dump(),
                "rejection_count": len(st_obj.rejection_history),
            })

    return {
        "active_dataset": reg.active_dataset,
        "selected_dataset": selected_ds,
        "datasets": {k: v.model_dump() for k, v in reg.datasets.items()},
        "summary": summary,
        "recorded_items": recorded_items,
        "admin": admin,
    }


class SetActiveDatasetRequest(BaseModel):
    dataset_name: str


@app.post("/api/admin/datasets/activate")
def admin_activate_dataset(req: SetActiveDatasetRequest, admin: dict = Depends(require_admin)):
    storage = get_storage()
    try:
        set_active_dataset(storage, req.dataset_name)
        return {"ok": True, "message": f"Đã kích hoạt '{req.dataset_name}' làm dataset đang thu chính thức."}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/admin/datasets/create")
async def admin_create_dataset(
    name: str = Form(...),
    display_name: str = Form(...),
    description: str = Form(""),
    manifest_file: UploadFile = File(...),
    admin: dict = Depends(require_admin),
):
    storage = get_storage()
    content_bytes = await manifest_file.read()
    ok, msg, errors = create_dataset(
        storage=storage,
        name=name.strip(),
        display_name=display_name.strip(),
        manifest_bytes=content_bytes,
        description=description.strip(),
    )
    if not ok:
        raise HTTPException(status_code=400, detail={"message": msg, "errors": errors[:30]})
    return {"ok": True, "message": msg}


@app.get("/api/admin/audio")
def admin_stream_audio(object_key: str, token: str):
    if token not in ADMIN_SESSIONS:
        raise HTTPException(status_code=401, detail="Unauthorized")
    storage = get_storage()
    try:
        audio_bytes, _ = storage.get_object(object_key)
        return Response(content=audio_bytes, media_type="audio/wav")
    except StorageNotFoundError:
        raise HTTPException(status_code=404, detail="Không tìm thấy tệp âm thanh.")


class ReviewDecisionRequest(BaseModel):
    dataset_name: str
    task_id: str
    expected_recording_id: str
    decision: str
    verified_transcript: Optional[str] = None
    error_reasons: List[str] = []
    note: Optional[str] = None


@app.post("/api/admin/review")
def admin_submit_review(req: ReviewDecisionRequest, admin: dict = Depends(require_admin)):
    storage = get_storage()
    reviewer_id = admin["reviewer_id"]
    reviewer_name = admin["reviewer_name"]

    lease_ok, lease_msg, lease_obj = claim_review_lease(
        storage, req.dataset_name, req.task_id, reviewer_id, reviewer_name
    )
    if not lease_ok or not lease_obj:
        raise HTTPException(status_code=409, detail=lease_msg)

    if req.decision in ("approve", "approve_edited"):
        if req.decision == "approve_edited" and not (req.verified_transcript and req.verified_transcript.strip()):
            raise HTTPException(status_code=400, detail="Văn bản sửa không được để trống.")
        ok, msg = approve_recording(
            storage=storage,
            dataset_name=req.dataset_name,
            task_id=req.task_id,
            review_lease_id=lease_obj.lease_id,
            reviewer_id=reviewer_id,
            reviewer_name=reviewer_name,
            verified_transcript=req.verified_transcript.strip() if req.decision == "approve_edited" else None,
            expected_recording_id=req.expected_recording_id,
        )
    elif req.decision == "reject":
        ok, msg = reject_recording(
            storage=storage,
            dataset_name=req.dataset_name,
            task_id=req.task_id,
            review_lease_id=lease_obj.lease_id,
            reviewer_id=reviewer_id,
            reviewer_name=reviewer_name,
            error_reasons=req.error_reasons,
            note=req.note,
            expected_recording_id=req.expected_recording_id,
        )
    else:
        raise HTTPException(status_code=400, detail="Quyết định không hợp lệ.")

    if not ok:
        raise HTTPException(status_code=409, detail=msg)
    return {"ok": True, "message": msg}


os.makedirs("static", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def serve_reader():
    return FileResponse("static/index.html")


@app.get("/admin")
def serve_admin():
    return FileResponse("static/admin.html")
