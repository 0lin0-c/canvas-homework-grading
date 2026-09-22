"""Qwen-VL wiring: vault/env key detection, mocked vision JSON, stub fallback."""
import base64
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.agent import GradingAgent
from app.attachment_render import attachments_to_page_images
from app.config import Settings
from app.models import AnswerSource, AttachmentMeta, CreateRunIn, StudentResult, SubmissionMeta
from app.multimodal_client import ModelCallError, chat_completion_text, parse_model_payload
from app.vault import ModelKeyVault

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
_FAKE_KEY = "unit-test-model-key-abcd"


def _settings(tmp_path: Path, **overrides) -> Settings:
    data = dict(
        grading_secret_key="unit-test-secret-key-32bytes-long!!",
        model_api_key=None,
        openai_api_key=None,
        dashscope_api_key=None,
        model_provider="dashscope",
        model_name="qwen3.8-max",
        model_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        model_vault_path=str(tmp_path / "model_api_key.enc"),
        model_max_pages=4,
        model_timeout_sec=5,
        model_confidence_floor=0.55,
    )
    data.update(overrides)
    return Settings(**data)


def _png(path: Path) -> None:
    path.write_bytes(_PNG)


def _student(image: Path | None) -> StudentResult:
    attachments = []
    if image is not None:
        size = image.stat().st_size if image.is_file() else 0
        attachments.append(
            AttachmentMeta(id="1", name=image.name, local_path=str(image), size=size)
        )
    return StudentResult(
        student_id="42",
        sortable_name="Test Student",
        submission=SubmissionMeta(
            workflow_state="submitted",
            submitted_at="2026-09-16T00:00:00Z",
            attachments=attachments,
        ),
    )


def _grade_payload(*, incorrect: int | None = 6, confidence: float = 0.95, needs_review: bool = False):
    questions = []
    for number in (1, 2, 6):
        status = "incorrect" if number == incorrect else "correct"
        questions.append(
            {
                "number": number,
                "status": status,
                "confidence": confidence,
                "evidence": f"page1 q{number}",
                "score": 10,
            }
        )
    return {
        "score": 60,
        "proposed_score": 10,
        "questions": questions,
        "comment_draft": "第6题符号错误",
        "overall_confidence": confidence,
        "needs_review": needs_review,
        "review_reasons": [],
        "flags": {
            "too_many_blank": False,
            "unreadable": False,
            "wrong_chapter": False,
            "attachments_fully_read": True,
        },
    }


def _patch_chat(monkeypatch, payload):
    captured = {}

    def fake(**kwargs):
        captured.update(kwargs)
        if isinstance(payload, Exception):
            raise payload
        if isinstance(payload, str):
            return payload
        return json.dumps(payload, ensure_ascii=False)

    monkeypatch.setattr("app.multimodal_client.chat_completion_text", fake)
    return captured


def test_has_model_key_env_and_vault(tmp_path):
    absent = _settings(tmp_path)
    assert absent.has_model_key is False

    by_model = _settings(tmp_path, model_api_key=_FAKE_KEY)
    assert by_model.has_model_key is True
    by_dashscope = _settings(tmp_path / "ds", dashscope_api_key=_FAKE_KEY)
    assert by_dashscope.has_model_key is True
    by_openai = _settings(tmp_path / "oa", openai_api_key=_FAKE_KEY)
    assert by_openai.has_model_key is True

    blank = _settings(tmp_path / "blank", model_api_key="   ")
    assert blank.has_model_key is False

    vault_settings = _settings(tmp_path / "vault")
    assert vault_settings.has_model_key is False
    vault = ModelKeyVault(vault_settings)
    saved = vault.save(_FAKE_KEY)
    assert saved["hint"] == "****abcd"
    assert _FAKE_KEY not in str(saved)
    raw = Path(vault_settings.model_vault_path).read_bytes()
    assert _FAKE_KEY.encode() not in raw
    assert vault_settings.has_model_key is True
    status = ModelKeyVault(vault_settings).status()
    assert set(status) == {"present", "hint", "validated_at"}
    assert status["present"] is True
    assert status["hint"] == "****abcd"
    assert _FAKE_KEY not in str(status)
    assert ModelKeyVault(vault_settings).get_key() == _FAKE_KEY
    ModelKeyVault(vault_settings).clear()
    assert vault_settings.has_model_key is False


def test_env_seeds_empty_vault_only(tmp_path):
    settings = _settings(tmp_path, dashscope_api_key=_FAKE_KEY)
    vault = ModelKeyVault(settings)
    assert vault.get_key() == _FAKE_KEY
    assert Path(settings.model_vault_path).is_file()
    assert _FAKE_KEY.encode() not in Path(settings.model_vault_path).read_bytes()


def test_parse_ignores_score_and_rejects_bad_json():
    parsed = parse_model_payload(json.dumps(_grade_payload()), [1, 2, 6])
    assert [q.status for q in parsed.questions] == ["correct", "correct", "incorrect"]
    assert not hasattr(parsed, "score")
    with pytest.raises(Exception):
        parse_model_payload("this is not json", [1, 2, 6])
    mismatched = _grade_payload()
    mismatched["questions"] = mismatched["questions"][:2]
    with pytest.raises(Exception):
        parse_model_payload(json.dumps(mismatched), [1, 2, 6])


def test_chat_retries_once_without_json_mode():
    calls = []

    class _Msg:
        content = '{"questions":[]}'

    class _Choice:
        message = _Msg()

    class _Resp:
        choices = [_Choice()]

    class _Completions:
        def create(self, **kwargs):
            calls.append(kwargs)
            if "response_format" in kwargs:
                raise RuntimeError("response_format unsupported")
            return _Resp()

    class _Chat:
        completions = _Completions()

    class _Client:
        chat = _Chat()

    text = chat_completion_text(
        api_key=_FAKE_KEY,
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        model="qwen3.8-max",
        messages=[{"role": "user", "content": "hi"}],
        timeout_sec=1,
        client=_Client(),
    )
    assert "questions" in text
    assert len(calls) == 1
    assert "response_format" not in calls[0]
    assert calls[0].get("extra_body", {}).get("enable_thinking") is False

    class _AlwaysFail:
        class chat:
            class completions:
                @staticmethod
                def create(**kwargs):
                    raise RuntimeError("sk-should-not-surface")

    with pytest.raises(ModelCallError) as exc:
        chat_completion_text(
            api_key=_FAKE_KEY,
            base_url="https://example.invalid/v1",
            model="qwen3.8-max",
            messages=[],
            timeout_sec=1,
            client=_AlwaysFail(),
        )
    assert "sk-should-not-surface" not in str(exc.value)
    assert _FAKE_KEY not in str(exc.value)


def test_mock_success_scores_from_rules_without_stub(tmp_path, monkeypatch):
    image = tmp_path / "work.png"
    _png(image)
    captured = _patch_chat(monkeypatch, _grade_payload())
    settings = _settings(tmp_path / "run", model_api_key=_FAKE_KEY)
    out = GradingAgent(settings).grade_student(
        _student(image),
        question_numbers=[1, 2, 6],
        answer_source=AnswerSource(imported_text="官方答案：第6题答案为42"),
    )
    assert out.status == "proposed"
    assert out.proposed_score == 99
    assert out.final_score == 99
    assert "扣分题号：6" in out.proposed_comment
    assert "第6题符号错误" in out.proposed_comment
    assert "stub_multimodal" not in out.review_reasons
    assert "model_hook_unwired" not in out.review_reasons
    assert _FAKE_KEY not in json.dumps(out.model_dump(), ensure_ascii=False)
    assert captured["model"] == "qwen3.8-max"
    assert captured["base_url"] == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    blob = json.dumps(captured["messages"])
    assert "image_url" in blob
    assert "data:image" in blob
    assert _FAKE_KEY not in blob
    assert "60" not in out.review_reasons


def test_model_needs_review_and_unreadable_clear_score(tmp_path, monkeypatch):
    image = tmp_path / "work.png"
    _png(image)
    payload = _grade_payload(incorrect=None, confidence=0.95, needs_review=True)
    payload["flags"]["unreadable"] = True
    payload["review_reasons"] = ["unreadable"]
    _patch_chat(monkeypatch, payload)
    settings = _settings(tmp_path / "flag", model_api_key=_FAKE_KEY)
    out = GradingAgent(settings).grade_student(
        _student(image),
        question_numbers=[1, 2, 6],
        answer_source=AnswerSource(imported_text="官方答案"),
    )
    assert out.status == "needs_review"
    assert out.proposed_score is None
    assert "unreadable" in out.review_reasons
    assert "stub_multimodal" not in out.review_reasons
    assert "model_hook_unwired" not in out.review_reasons


def test_bad_json_needs_review(tmp_path, monkeypatch):
    image = tmp_path / "work.png"
    _png(image)
    _patch_chat(monkeypatch, "NOT JSON {{{")
    settings = _settings(tmp_path / "bad", model_api_key=_FAKE_KEY)
    out = GradingAgent(settings).grade_student(
        _student(image),
        question_numbers=[1, 2, 6],
        answer_source=AnswerSource(imported_text="官方答案"),
    )
    assert out.status == "needs_review"
    assert out.proposed_score is None
    assert out.final_score is None
    assert "model_parse_error" in out.review_reasons
    assert "stub_multimodal" not in out.review_reasons
    assert "model_hook_unwired" not in out.review_reasons


def test_low_confidence_forces_review(tmp_path, monkeypatch):
    image = tmp_path / "work.png"
    _png(image)
    _patch_chat(monkeypatch, _grade_payload(incorrect=None, confidence=0.54))
    settings = _settings(tmp_path / "low", model_api_key=_FAKE_KEY)
    out = GradingAgent(settings).grade_student(
        _student(image),
        question_numbers=[1, 2, 6],
        answer_source=AnswerSource(imported_text="官方答案"),
    )
    assert out.status == "needs_review"
    assert out.proposed_score is None
    assert "low_confidence" in out.review_reasons
    assert "stub_multimodal" not in out.review_reasons


def test_confidence_at_floor_can_propose(tmp_path, monkeypatch):
    image = tmp_path / "work.png"
    _png(image)
    _patch_chat(monkeypatch, _grade_payload(incorrect=None, confidence=0.55))
    settings = _settings(tmp_path / "floor", model_api_key=_FAKE_KEY)
    out = GradingAgent(settings).grade_student(
        _student(image),
        question_numbers=[1, 2, 6],
        answer_source=AnswerSource(imported_text="官方答案"),
    )
    assert out.status == "proposed"
    assert out.proposed_score == 100
    assert "low_confidence" not in out.review_reasons


def test_model_call_failed_uses_stub_semantics(tmp_path, monkeypatch):
    image = tmp_path / "work.png"
    _png(image)
    _patch_chat(monkeypatch, RuntimeError("sk-should-not-surface"))
    settings = _settings(tmp_path / "fail", model_api_key=_FAKE_KEY)
    out = GradingAgent(settings).grade_student(
        _student(image),
        question_numbers=[1, 2, 6],
        answer_source=AnswerSource(imported_text="官方答案"),
    )
    assert out.status == "needs_review"
    assert out.proposed_score is None
    assert "model_call_failed" in out.review_reasons
    assert "stub_multimodal" in out.review_reasons
    dumped = json.dumps(out.model_dump(), ensure_ascii=False)
    assert "sk-should-not-surface" not in dumped
    assert _FAKE_KEY not in dumped


def test_no_key_does_not_call_model(tmp_path, monkeypatch):
    def boom(**kwargs):
        raise AssertionError("model should not be called")

    monkeypatch.setattr("app.multimodal_client.chat_completion_text", boom)
    settings = _settings(tmp_path)
    out = GradingAgent(settings).grade_student(
        _student(tmp_path / "missing.png"),
        question_numbers=[1, 2, 6],
        answer_source=AnswerSource(imported_text="官方答案"),
    )
    assert out.status == "needs_review"
    assert out.proposed_score is None
    assert "stub_multimodal" in out.review_reasons


def test_no_official_answer_skips_model_even_with_key(tmp_path, monkeypatch):
    def boom(**kwargs):
        raise AssertionError("model should not be called")

    monkeypatch.setattr("app.multimodal_client.chat_completion_text", boom)
    image = tmp_path / "work.png"
    _png(image)
    settings = _settings(tmp_path / "ans", model_api_key=_FAKE_KEY)
    out = GradingAgent(settings).grade_student(
        _student(image),
        question_numbers=[1, 2, 6],
        answer_source=AnswerSource(imported_text="  "),
    )
    assert out.status == "needs_review"
    assert out.proposed_score is None
    assert "no_official_answer" in out.review_reasons
    assert "stub_multimodal" in out.review_reasons


def test_render_image_and_pdf(tmp_path):
    img = tmp_path / "scan.png"
    _png(img)
    pdf = tmp_path / "scan.pdf"
    Image.new("RGB", (30, 30), "white").save(pdf, "PDF")
    missing = tmp_path / "gone.pdf"
    images, fully = attachments_to_page_images(
        [str(img), str(pdf)],
        max_pages=5,
        work_dir=tmp_path / "pages",
    )
    assert fully is True
    assert images[0] == img
    assert images[1].suffix == ".png"
    assert len(images) == 2
    capped, capped_full = attachments_to_page_images(
        [str(img), str(pdf)],
        max_pages=1,
        work_dir=tmp_path / "cap",
    )
    assert capped_full is False
    assert len(capped) == 1
    partial, partial_full = attachments_to_page_images(
        [str(img), str(missing)],
        max_pages=5,
        work_dir=tmp_path / "partial",
    )
    assert partial_full is False
    assert partial == [img]


def test_create_run_downloads_attachments_by_default():
    body = CreateRunIn(assignment_url="https://oc.sjtu.edu.cn/courses/94139/assignments/428317")
    assert body.download_attachments is True


def test_model_credential_api_hides_key(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    vault = ModelKeyVault(settings)
    monkeypatch.setattr("app.main.get_model_vault", lambda: vault)
    from app.main import app

    client = TestClient(app)
    saved = client.post("/api/credentials/model", json={"api_key": _FAKE_KEY})
    assert saved.status_code == 200
    body = saved.json()
    assert set(body) == {"present", "hint", "validated_at"}
    assert body["hint"] == "****abcd"
    assert _FAKE_KEY not in saved.text
    fetched = client.get("/api/credentials/model")
    assert fetched.status_code == 200
    assert fetched.json()["hint"] == "****abcd"
    assert _FAKE_KEY not in fetched.text
    cleared = client.delete("/api/credentials/model")
    assert cleared.json() == {"cleared": True}
    after = client.get("/api/credentials/model")
    assert after.json()["present"] is False
    assert _FAKE_KEY not in after.text


def test_index_model_key_field_and_download_default():
    from app.main import app

    client = TestClient(app)
    html = client.get("/").text
    assert 'id="model-key-form"' in html
    assert 'name="api_key"' in html
    assert 'type="password"' in html
    assert 'id="dl-att"' in html
    assert 'id="dl-att" checked' in html
    assert _FAKE_KEY not in html
    assert "qwen3.8-max" in html
