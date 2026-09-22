"""FastAPI entry: Canvas TA grading app V1."""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import __version__
from app.agent import GradingAgent
from app.canvas_client import (
    CanvasAPIError,
    CanvasAuthError,
    CanvasClient,
    normalize_submission,
    resolve_assignment_url,
)
from app.config import get_settings
from app.models import (
    AnswerImportIn,
    AnswerSource,
    AttachmentMeta,
    CreateRunIn,
    CredentialIn,
    CredentialStatus,
    GradingRun,
    PlatformSnapshot,
    ResolveAssignmentIn,
    ResolveAssignmentOut,
    StudentPatchIn,
    StudentResult,
    SubmissionMeta,
    WritebackIn,
)
from app.question_bank import get_question_bank
from app.scoring import RULES_VERSION
from app.store import get_store, new_run_id
from app.vault import get_vault
from app.writeback import WritebackService, can_writeback

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
# Redact Authorization in uvicorn access if present
logging.getLogger("httpx").setLevel(logging.WARNING)

app = FastAPI(title="Canvas TA Grading V1", version=__version__)
settings = get_settings()
BASE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")


# --------------- helpers ---------------


def _client() -> CanvasClient:
    return CanvasClient()


def _question_numbers_for(course_id: int, assignment_id: int) -> list[int]:
    if (
        course_id == settings.sample_course_id
        and assignment_id == settings.sample_assignment_id
    ):
        return list(settings.default_question_numbers)
    return list(settings.default_question_numbers)


# --------------- pages ---------------


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    vault_status = get_vault().status()
    qb = get_question_bank().status()
    runs = get_store().list_runs(20)
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "vault": vault_status,
            "question_bank": qb,
            "runs": runs,
            "writeback_max_batch": settings.writeback_max_batch,
            "has_model_key": settings.has_model_key,
            "host": settings.host,
            "port": settings.port,
            "version": __version__,
        },
    )


@app.get("/runs/{run_id}", response_class=HTMLResponse)
async def run_page(request: Request, run_id: str) -> HTMLResponse:
    run = get_store().get_run(run_id)
    if not run:
        raise HTTPException(404, "run not found")
    return templates.TemplateResponse(
        request,
        "run.html",
        {
            "run": run,
            "writeback_max_batch": settings.writeback_max_batch,
            "version": __version__,
        },
    )


# --------------- credentials ---------------


@app.get("/api/credentials/status", response_model=CredentialStatus)
def credentials_status() -> CredentialStatus:
    st = get_vault().status()
    return CredentialStatus(**st)


@app.post("/api/credentials", response_model=CredentialStatus)
def credentials_save(body: CredentialIn) -> CredentialStatus:
    token = body.token.strip()
    if not token:
        raise HTTPException(400, "empty token")
    # Validate against Canvas before saving permanently
    vault = get_vault()
    # Temporarily hold for validation
    prev = vault.get_token()
    try:
        vault.save(token)
        client = _client()
        me = client.get_self()
        name = me.get("name")
        st = vault.status()
        return CredentialStatus(
            present=True,
            hint=st.get("hint"),
            validated_at=st.get("validated_at"),
            valid=True,
            user_name=name,
        )
    except CanvasAuthError:
        vault.clear()
        if prev:
            try:
                vault.save(prev)
            except Exception:
                pass
        raise HTTPException(401, "Canvas token invalid")
    except CanvasAPIError as exc:
        vault.clear()
        if prev:
            try:
                vault.save(prev)
            except Exception:
                pass
        raise HTTPException(502, f"Canvas error HTTP {exc.status_code}")
    except Exception as exc:  # noqa: BLE001
        # Network etc. — still save if user insists? Prefer fail closed for V1
        logging.getLogger(__name__).warning("Token validate failed: %s", type(exc).__name__)
        # Keep saved token for offline; mark unvalidated path
        st = vault.status()
        return CredentialStatus(
            present=True,
            hint=st.get("hint"),
            validated_at=st.get("validated_at"),
            valid=False,
            user_name=None,
        )


@app.delete("/api/credentials")
def credentials_clear() -> dict[str, bool]:
    get_vault().clear()
    return {"cleared": True}


# --------------- assignments ---------------


@app.post("/api/assignments/resolve", response_model=ResolveAssignmentOut)
def assignments_resolve(body: ResolveAssignmentIn) -> ResolveAssignmentOut:
    try:
        ids = resolve_assignment_url(body.url, allowed_host=settings.canvas_host)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    name = None
    points = None
    due = None
    types: list[str] = []
    try:
        if get_vault().get_token():
            asg = _client().get_assignment(ids["course_id"], ids["assignment_id"])
            name = asg.get("name")
            points = asg.get("points_possible")
            due = asg.get("due_at")
            types = asg.get("submission_types") or []
    except (CanvasAuthError, CanvasAPIError):
        pass

    return ResolveAssignmentOut(
        course_id=ids["course_id"],
        assignment_id=ids["assignment_id"],
        name=name,
        points_possible=points,
        due_at=due,
        submission_types=types,
        question_numbers=_question_numbers_for(ids["course_id"], ids["assignment_id"]),
    )


# --------------- question bank / answers ---------------


@app.get("/api/question-bank/status")
def qb_status() -> dict[str, Any]:
    return get_question_bank().status()


@app.post("/api/question-bank/answers")
def qb_import_answers(body: AnswerImportIn) -> dict[str, Any]:
    if not body.text or not body.text.strip():
        raise HTTPException(400, "text required")
    src = get_question_bank().import_answer_text(
        body.text,
        chapter=body.chapter,
        resource_url=body.resource_url,
        pages_read=body.pages_read,
    )
    return {"ok": True, "answer_source": src.model_dump()}


@app.post("/api/question-bank/answers/upload")
async def qb_upload_answers(
    file: UploadFile = File(...),
    chapter: str = Form("习题一"),
) -> dict[str, Any]:
    raw = await file.read()
    text = raw.decode("utf-8", errors="replace")
    src = get_question_bank().import_answer_text(text, chapter=chapter)
    return {"ok": True, "answer_source": src.model_dump()}


# --------------- runs ---------------


@app.get("/api/runs")
def list_runs() -> list[dict[str, Any]]:
    return get_store().list_runs()


@app.post("/api/runs")
def create_run(body: CreateRunIn) -> dict[str, Any]:
    try:
        ids = resolve_assignment_url(body.assignment_url, allowed_host=settings.canvas_host)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    if not get_vault().get_token():
        raise HTTPException(401, "Canvas token required")

    client = _client()
    try:
        asg = client.get_assignment(ids["course_id"], ids["assignment_id"])
        raw_subs = client.list_submissions(ids["course_id"], ids["assignment_id"])
    except CanvasAuthError:
        raise HTTPException(401, "Canvas auth failed") from None
    except CanvasAPIError as exc:
        raise HTTPException(502, f"Canvas HTTP {exc.status_code}") from exc

    qnums = body.question_numbers or _question_numbers_for(
        ids["course_id"], ids["assignment_id"]
    )
    qb = get_question_bank()
    answer_text = body.answer_text or qb.load_imported_text("习题一")
    answer_source = AnswerSource(
        chapter="习题一",
        resource_url=None,
        imported_text=answer_text,
        source_type="ta_import" if answer_text else "none",
    )

    run_id = new_run_id()
    work_att = settings.repo_root() / "work" / "runs" / run_id / "attachments"
    students: list[StudentResult] = []

    for raw in raw_subs:
        norm = normalize_submission(raw)
        atts = []
        for a in norm["submission"]["attachments"]:
            local_path = None
            if body.download_attachments and a.get("url"):
                try:
                    dest = work_att / f"{norm['student_id']}_{a['id']}_{a.get('name') or 'file'}"
                    # sanitize filename
                    dest = work_att / re.sub(r"[^\w.\-]+", "_", dest.name)
                    client.download_attachment(a["url"], dest)
                    local_path = str(dest)
                except Exception:
                    logging.getLogger(__name__).warning(
                        "attachment download failed student=%s aid=%s",
                        norm["student_id"],
                        a.get("id"),
                    )
            atts.append(
                AttachmentMeta(
                    id=str(a["id"]),
                    name=a.get("name") or "",
                    url=None,  # do not persist signed URLs
                    size=a.get("size"),
                    local_path=local_path,
                )
            )
        sub = SubmissionMeta(
            submitted_at=norm["submission"]["submitted_at"],
            attempt=norm["submission"]["attempt"],
            workflow_state=norm["submission"]["workflow_state"],
            attachments=atts,
            fingerprint=norm["submission"]["fingerprint"],
        )
        pb = PlatformSnapshot(**norm["platform_before"])
        status = "pending"
        if not norm["has_submission"]:
            status = "skipped"
        students.append(
            StudentResult(
                student_id=norm["student_id"],
                student_name=norm.get("student_name"),
                sortable_name=norm.get("sortable_name"),
                submission=sub,
                platform_before=pb,
                status=status,  # type: ignore[arg-type]
            )
        )

    run = GradingRun(
        id=run_id,
        mode=body.mode,
        assignment_url=body.assignment_url,
        course_id=ids["course_id"],
        assignment_id=ids["assignment_id"],
        assignment_name=asg.get("name"),
        points_possible=asg.get("points_possible"),
        rules_version=RULES_VERSION,
        question_numbers=qnums,
        answer_source=answer_source,
        students=students,
        status="fetched",
    )
    get_store().create_run(run)
    return run.model_dump()


@app.get("/api/runs/{run_id}")
def get_run(run_id: str) -> dict[str, Any]:
    run = get_store().get_run(run_id)
    if not run:
        raise HTTPException(404, "not found")
    return run.model_dump()


@app.post("/api/runs/{run_id}/grade")
def grade_run(run_id: str) -> dict[str, Any]:
    store = get_store()
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(404, "not found")
    # Refresh answer text if imported after create
    imported = get_question_bank().load_imported_text(run.answer_source.chapter)
    if imported:
        run.answer_source.imported_text = imported
        run.answer_source.source_type = "ta_import"

    agent = GradingAgent()
    run.students = agent.grade_run_students(
        run.students,
        question_numbers=run.question_numbers,
        answer_source=run.answer_source,
    )
    run.status = "graded"
    store.save_run(run)
    return run.model_dump()


@app.get("/api/runs/{run_id}/students/{sid}")
def get_student(run_id: str, sid: str) -> dict[str, Any]:
    st = get_store().get_student(run_id, sid)
    if not st:
        raise HTTPException(404, "not found")
    return st.model_dump()


@app.patch("/api/runs/{run_id}/students/{sid}")
def patch_student(run_id: str, sid: str, body: StudentPatchIn) -> dict[str, Any]:
    store = get_store()
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(404, "run not found")
    st = next((s for s in run.students if s.student_id == sid), None)
    if not st:
        raise HTTPException(404, "student not found")
    if body.final_score is not None:
        st.final_score = body.final_score
    if body.final_comment is not None:
        st.final_comment = body.final_comment
    if body.force_authorized is not None:
        st.force_authorized = body.force_authorized
        if body.force_authorized and st.status == "needs_review":
            # Allow writeback path after TA release
            if st.final_score is None and st.proposed_score is not None:
                st.final_score = st.proposed_score
            st.status = "proposed"
    if body.status is not None:
        st.status = body.status
    store.save_run(run)
    return st.model_dump()


@app.post("/api/runs/{run_id}/writeback")
def writeback_run(run_id: str, body: WritebackIn) -> dict[str, Any]:
    store = get_store()
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(404, "not found")
    if run.mode == "rehearsal":
        raise HTTPException(400, "rehearsal mode cannot writeback")
    if not body.confirm:
        raise HTTPException(400, "confirm=true required")

    svc = WritebackService()
    dry = run.mode == "suggest"  # suggest mode: dry-run only
    # Actually: product says suggest does not write. Force dry for suggest.
    if run.mode != "writeback":
        # Allow explicit writeback only in writeback mode; still support dry preview
        results = svc.writeback_batch(
            course_id=run.course_id,
            assignment_id=run.assignment_id,
            students=run.students,
            student_ids=body.student_ids,
            rules_version=run.rules_version,
            confirm=body.confirm,
            dry_run=True,
        )
        store.save_run(run)
        return {
            "dry_run": True,
            "hint": "Set run mode=writeback to perform real Canvas writeback",
            "results": [r.model_dump() for r in results],
            "max_batch": settings.writeback_max_batch,
        }

    results = svc.writeback_batch(
        course_id=run.course_id,
        assignment_id=run.assignment_id,
        students=run.students,
        student_ids=body.student_ids,
        rules_version=run.rules_version,
        confirm=body.confirm,
        dry_run=False,
    )
    run.status = "writeback"
    store.save_run(run)
    return {
        "dry_run": False,
        "results": [r.model_dump() for r in results],
        "max_batch": settings.writeback_max_batch,
    }


@app.post("/api/runs/{run_id}/mode")
def set_run_mode(run_id: str, mode: str = Form(...)) -> dict[str, Any]:
    if mode not in ("suggest", "writeback", "rehearsal"):
        raise HTTPException(400, "invalid mode")
    store = get_store()
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(404, "not found")
    run.mode = mode  # type: ignore[assignment]
    store.save_run(run)
    return {"id": run.id, "mode": run.mode}


@app.post("/api/runs/{run_id}/writeback/{sid}/retry")
def writeback_retry(run_id: str, sid: str) -> dict[str, Any]:
    store = get_store()
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(404, "not found")
    if run.mode != "writeback":
        raise HTTPException(400, "run mode must be writeback")
    st = next((s for s in run.students if s.student_id == sid), None)
    if not st:
        raise HTTPException(404, "student not found")
    svc = WritebackService()
    result = svc.writeback_student(
        course_id=run.course_id,
        assignment_id=run.assignment_id,
        student=st,
        rules_version=run.rules_version,
        dry_run=False,
    )
    store.save_run(run)
    return result.model_dump()


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "version": __version__,
        "host": settings.host,
        "writeback_max_batch": settings.writeback_max_batch,
        "vault_present": get_vault().status().get("present"),
        "has_model_key": settings.has_model_key,
    }


def create_app() -> FastAPI:
    return app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
    )
