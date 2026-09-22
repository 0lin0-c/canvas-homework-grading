"""Grade entry blocks placeholder answers; PATCH reports comment validation."""
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.models import (
    AnswerSource,
    AttachmentMeta,
    GradingRun,
    QuestionItem,
    StudentResult,
    SubmissionMeta,
)
from app.scoring import RULES_VERSION
from app.store import RunStore

LONG_ANSWER = "第6题\nP(A∪B)=P(A)+P(B)-P(A∩B)，互斥时去掉相交项。\n"


class FakeBank:
    def __init__(self, text):
        self.text = text
        self.imports = []

    def load_imported_text(self, chapter="习题一"):
        return self.text

    def import_answer_text(self, text, chapter="习题一", resource_url=None, pages_read=None):
        self.imports.append(text)
        return AnswerSource(
            chapter=chapter,
            imported_text=text,
            resource_url=resource_url,
            pages_read=pages_read or [],
            source_type="ta_import",
        )


class RecordingAgent:
    def __init__(self, calls):
        self.calls = calls

    def grade_run_students(self, students, **kwargs):
        self.calls.append(kwargs.get("answer_source"))
        return students


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        grading_secret_key="unit-test-secret-key-32bytes-long!!",
        database_path=str(tmp_path / "grading.db"),
        vault_path=str(tmp_path / "canvas.enc"),
        model_vault_path=str(tmp_path / "model.enc"),
        model_api_key=None,
        openai_api_key=None,
        dashscope_api_key=None,
    )


def _run(answer: str | None, sid: str = "42") -> GradingRun:
    return GradingRun(
        id=f"gate{sid}",
        mode="suggest",
        assignment_url="https://oc.sjtu.edu.cn/courses/1/assignments/2",
        course_id=1,
        assignment_id=2,
        rules_version=RULES_VERSION,
        question_numbers=[1, 6],
        answer_source=AnswerSource(
            imported_text=answer,
            source_type="ta_import" if answer else "none",
        ),
        students=[
            StudentResult(
                student_id=sid,
                submission=SubmissionMeta(
                    workflow_state="submitted",
                    submitted_at="2026-09-16T00:00:00Z",
                    attachments=[AttachmentMeta(id="1", name="a.png", local_path="/tmp/a.png")],
                ),
                questions=[
                    QuestionItem(number=1, status="correct"),
                    QuestionItem(number=6, status="incorrect"),
                ],
                status="pending",
            )
        ],
        status="fetched",
    )


def _client(monkeypatch, store: RunStore, bank: FakeBank, calls: list):
    monkeypatch.setattr("app.main.get_store", lambda: store)
    monkeypatch.setattr("app.main.get_question_bank", lambda: bank)
    monkeypatch.setattr("app.main.GradingAgent", lambda: RecordingAgent(calls))
    from app.main import app

    return TestClient(app)


def _cleanup(run_id: str) -> None:
    path = Path("outputs") / f"run-{run_id}.json"
    if path.is_file():
        path.unlink()


def test_grade_rejects_lue_and_todo_without_calling_agent(tmp_path, monkeypatch):
    store = RunStore(_settings(tmp_path))
    calls: list = []
    client = _client(monkeypatch, store, FakeBank(None), calls)
    for answer in ("略", "TODO: fill later", "甲" * (19)):
        run = _run(answer, sid=str(abs(hash(answer)) % 100000))
        store.create_run(run)
        try:
            response = client.post(f"/api/runs/{run.id}/grade")
            assert response.status_code == 400
            assert response.json()["detail"] == "placeholder_answer_replace_required"
            saved = store.get_run(run.id)
            assert saved is not None
            assert saved.status == "fetched"
            assert calls == []
        finally:
            _cleanup(run.id)


def test_grade_allows_missing_answer_and_real_answer(tmp_path, monkeypatch):
    store = RunStore(_settings(tmp_path))
    calls: list = []
    bank = FakeBank(None)
    client = _client(monkeypatch, store, bank, calls)
    missing = _run(None, sid="100")
    blank = _run("   ", sid="101")
    real = _run(LONG_ANSWER, sid="102")
    for run in (missing, blank, real):
        store.create_run(run)
    try:
        for run in (missing, blank, real):
            response = client.post(f"/api/runs/{run.id}/grade")
            assert response.status_code == 200, response.text
            assert response.json()["status"] == "graded"
        assert len(calls) == 3
    finally:
        for run in (missing, blank, real):
            _cleanup(run.id)


def test_imported_placeholder_overrides_stored_answer(tmp_path, monkeypatch):
    store = RunStore(_settings(tmp_path))
    calls: list = []
    client = _client(monkeypatch, store, FakeBank("略"), calls)
    run = _run(LONG_ANSWER, sid="200")
    store.create_run(run)
    try:
        response = client.post(f"/api/runs/{run.id}/grade")
        assert response.status_code == 400
        assert response.json()["detail"] == "placeholder_answer_replace_required"
        assert calls == []
    finally:
        _cleanup(run.id)


def test_replaced_real_answer_allows_grade(tmp_path, monkeypatch):
    store = RunStore(_settings(tmp_path))
    calls: list = []
    client = _client(monkeypatch, store, FakeBank(LONG_ANSWER), calls)
    run = _run("略", sid="201")
    store.create_run(run)
    try:
        response = client.post(f"/api/runs/{run.id}/grade")
        assert response.status_code == 200
        assert len(calls) == 1
    finally:
        _cleanup(run.id)


def test_answer_import_warning_does_not_use_local_files(monkeypatch):
    bank = FakeBank(None)
    monkeypatch.setattr("app.main.get_question_bank", lambda: bank)
    from app.main import app

    client = TestClient(app)
    bad = client.post("/api/question-bank/answers", json={"text": "略", "chapter": "习题一"})
    assert bad.status_code == 200
    assert bad.json()["warning"] == "placeholder_detected"
    assert bank.imports == ["略"]

    good = client.post(
        "/api/question-bank/answers",
        json={"text": LONG_ANSWER, "chapter": "习题一"},
    )
    assert good.status_code == 200
    assert good.json()["warning"] is None

    todo = client.post(
        "/api/question-bank/answers",
        json={"text": "TODO: fill later", "chapter": "习题一"},
    )
    assert todo.status_code == 200
    assert todo.json()["warning"] == "placeholder_detected"

    empty = client.post("/api/question-bank/answers", json={"text": "  "})
    assert empty.status_code == 400

    uploaded = client.post(
        "/api/question-bank/answers/upload",
        files={"file": ("ans.txt", "略".encode(), "text/plain")},
        data={"chapter": "单元测试章"},
    )
    assert uploaded.status_code == 200
    assert uploaded.json()["warning"] == "placeholder_detected"
    assert "略" in bank.imports


def test_patch_returns_comment_validation(tmp_path, monkeypatch):
    store = RunStore(_settings(tmp_path))
    calls: list = []
    client = _client(monkeypatch, store, FakeBank(LONG_ANSWER), calls)
    run = _run(LONG_ANSWER, sid="300")
    run.students[0].status = "proposed"
    run.students[0].proposed_score = 99
    run.students[0].final_score = 99
    run.students[0].proposed_comment = "第6题：错了"
    run.students[0].final_comment = "第6题：错了"
    store.create_run(run)
    try:
        bad = client.patch(
            f"/api/runs/{run.id}/students/300",
            json={"final_comment": "第6题：错了"},
        )
        assert bad.status_code == 200
        body = bad.json()
        assert body["final_comment"] == "第6题：错了"
        assert body["comment_validation"]["ok"] is False
        assert "R_CORRECT_POINT_HEURISTIC" in body["comment_validation"]["rule_ids"]
        assert body["comment_validation"]["messages"]

        good = client.patch(
            f"/api/runs/{run.id}/students/300",
            json={"final_comment": "第6题：符号用反，正确应为 P(A∪B)=P(A)+P(B)-P(A∩B)。"},
        )
        assert good.status_code == 200
        assert good.json()["comment_validation"]["ok"] is True
        assert good.json()["comment_validation"]["rule_ids"] == []
    finally:
        _cleanup(run.id)
