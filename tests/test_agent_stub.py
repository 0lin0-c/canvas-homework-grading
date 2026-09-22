"""Stub multimodal must not emit trusted/full proposed scores."""
from app.agent import GradingAgent, stub_multimodal_grade
from app.config import Settings
from app.models import AnswerSource, AttachmentMeta, StudentResult, SubmissionMeta


def test_stub_with_answers_and_paths_forces_uncertain():
    qs, reasons = stub_multimodal_grade(
        question_numbers=[1, 2, 6],
        answer_text="官方答案全文……",
        attachment_paths=["/tmp/fake.pdf"],
        student_id="u1",
    )
    assert "stub_multimodal" in reasons
    assert all(q.status == "uncertain" for q in qs)


def test_grade_student_stub_needs_review_no_full_score():
    settings = Settings(model_api_key=None, openai_api_key=None)
    agent = GradingAgent(settings)
    student = StudentResult(
        student_id="42",
        sortable_name="Test Student",
        submission=SubmissionMeta(
            user_id=42,
            workflow_state="submitted",
            submitted_at="2026-09-16T00:00:00Z",
            attachments=[
                AttachmentMeta(id="1", filename="a.pdf", local_path="/tmp/a.pdf", size=10)
            ],
        ),
    )
    out = agent.grade_student(
        student,
        question_numbers=[1, 2, 6, 7, 8, 10, 11, 12, 15],
        answer_source=AnswerSource(imported_text="习题一答案……"),
    )
    assert out.status == "needs_review"
    assert out.proposed_score is None
    assert "stub_multimodal" in out.review_reasons
    assert out.final_score is None
