"""Agent grading worker: deterministic rule engine + Qwen-VL multimodal hook."""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.attachment_render import attachments_to_page_images
from app.config import Settings, get_settings
from app.models import AnswerSource, QuestionItem, StudentResult
from app.multimodal_client import (
    ModelParseError,
    VisionGrade,
    grade_images,
)
from app.scoring import (
    RULES_VERSION,
    QuestionResult,
    evaluate_questions,
)
from app.vault import ModelKeyVault

logger = logging.getLogger(__name__)


def stub_multimodal_grade(
    *,
    question_numbers: list[int],
    answer_text: str | None,
    attachment_paths: list[str],
    student_id: str,
) -> tuple[list[QuestionResult], list[str]]:
    """
    Stub multimodal hook when no real model is wired.

    Hard rule (code review): stub MUST NOT emit trusted/full scores.
    All questions are marked uncertain → caller forces needs_review.
    Optional INCORRECT/MISSING/UNCERTAIN lines are only for local fixtures
    and still do not authorize writeback-eligible proposed scores.
    """
    reasons: list[str] = ["stub_multimodal"]
    if not answer_text or not answer_text.strip():
        reasons.append("no_official_answer")
        qs = [
            QuestionResult(
                number=n,
                status="uncertain",
                evidence="stub:no_official_answer_imported",
                confidence=0.05,
            )
            for n in question_numbers
        ]
        return qs, reasons

    if not attachment_paths:
        reasons.append("no_attachment_pages")

    # Never treat stub as having "read" the submission: all uncertain.
    # Fixture directives may annotate evidence only; statuses stay uncertain.
    qs = [
        QuestionResult(
            number=n,
            status="uncertain",
            evidence="stub:no_real_multimodal_read",
            confidence=0.05,
        )
        for n in question_numbers
    ]

    # Preserve fixture hints in evidence for debugging (does not change status)
    m = re.search(r"INCORRECT\s*:\s*([\d,\s]+)", answer_text or "", re.I)
    if m:
        hinted = {int(x.strip()) for x in m.group(1).split(",") if x.strip().isdigit()}
        for q in qs:
            if q.number in hinted:
                q.evidence = "stub:fixture_hint_incorrect"
    if student_id:
        for line in (answer_text or "").splitlines():
            if student_id in line and re.search(r"INCORRECT\s*:", line, re.I):
                m2 = re.search(r"INCORRECT\s*:\s*([\d,\s]+)", line, re.I)
                if m2:
                    hinted = {
                        int(x.strip()) for x in m2.group(1).split(",") if x.strip().isdigit()
                    }
                    for q in qs:
                        if q.number in hinted:
                            q.evidence = "stub:fixture_hint_incorrect"
    return qs, reasons


# Tokens that force needs_review and clear proposed_score (delta §5).
_CLEAR_SCORE_TOKENS = {
    "stub_multimodal",
    "model_hook_unwired",
    "no_official_answer",
    "no_attachment_pages",
    "model_call_failed",
    "model_parse_error",
    "format_invalid",
    "low_confidence",
    "unread_attachments",
    "too_many_blank",
    "unreadable",
    "unrecognizable",
    "suspected_wrong_chapter",
    "few_missing_policy_unclear",
}
_STUB_TOKENS = {"stub_multimodal", "model_hook_unwired"}
_STUB_COMMENT = "【待复核】未接入真实多模态批改（stub），请助教人工评分与评语。"


@dataclass
class MultimodalOutcome:
    questions: list[QuestionResult]
    reasons: list[str]
    comment_draft: str = ""
    model_needs_review: bool = False
    overall_confidence: float | None = None
    flags: dict[str, Any] = field(default_factory=dict)
    real_success: bool = False


def _append_reason(reasons: list[str], token: str) -> None:
    if token and token not in reasons:
        reasons.append(token)


def _uncertain(question_numbers: list[int], evidence: str) -> list[QuestionResult]:
    return [
        QuestionResult(number=n, status="uncertain", evidence=evidence, confidence=0.0)
        for n in question_numbers
    ]


def _apply_model_flags(reasons: list[str], flags: dict[str, Any]) -> None:
    if flags.get("too_many_blank") is True:
        _append_reason(reasons, "too_many_blank")
    if flags.get("unreadable") is True:
        _append_reason(reasons, "unreadable")
    if flags.get("wrong_chapter") is True:
        _append_reason(reasons, "suspected_wrong_chapter")
    if flags.get("attachments_fully_read") is False:
        _append_reason(reasons, "unread_attachments")


def _confidence_below_floor(vision: VisionGrade, floor: float) -> bool:
    if vision.overall_confidence < floor:
        return True
    return any(q.confidence < floor for q in vision.questions)


def _stub_outcome(
    *,
    question_numbers: list[int],
    answer_text: str | None,
    attachment_paths: list[str],
    student_id: str,
    extra: list[str] | None = None,
) -> MultimodalOutcome:
    questions, reasons = stub_multimodal_grade(
        question_numbers=question_numbers,
        answer_text=answer_text,
        attachment_paths=attachment_paths,
        student_id=student_id,
    )
    reasons = list(reasons)
    for token in extra or []:
        _append_reason(reasons, token)
    return MultimodalOutcome(questions=questions, reasons=reasons, real_success=False)


def multimodal_grade_with_model(
    *,
    question_numbers: list[int],
    answer_text: str | None,
    attachment_paths: list[str],
    student_id: str,
    api_key: str,
    settings: Settings | None = None,
    download_incomplete: bool = False,
) -> MultimodalOutcome:
    """Call Qwen-VL (DashScope compatible-mode) and map JSON to QuestionResult.

    On HTTP/SDK/timeout/empty failure, fall back to stub semantics plus
    model_call_failed. A successful parse never includes stub_multimodal or
    model_hook_unwired. Model score fields are ignored by the parser.
    """
    settings = settings or get_settings()
    if not answer_text or not answer_text.strip():
        return _stub_outcome(
            question_numbers=question_numbers,
            answer_text=answer_text,
            attachment_paths=attachment_paths,
            student_id=student_id,
        )
    if not api_key or not str(api_key).strip():
        return _stub_outcome(
            question_numbers=question_numbers,
            answer_text=answer_text,
            attachment_paths=attachment_paths,
            student_id=student_id,
            extra=["model_call_failed"],
        )

    safe_id = re.sub(r"[^\w.\-]+", "_", student_id or "student")[:80] or "student"
    work = settings.repo_root() / "work" / "page-images" / safe_id
    try:
        images, fully = attachments_to_page_images(
            attachment_paths,
            max_pages=int(settings.model_max_pages),
            work_dir=work,
        )
    except Exception:
        logger.warning("attachment render failed student=%s", student_id)
        return _stub_outcome(
            question_numbers=question_numbers,
            answer_text=answer_text,
            attachment_paths=attachment_paths,
            student_id=student_id,
            extra=["model_call_failed"],
        )

    reasons: list[str] = []
    if not attachment_paths or not images:
        _append_reason(reasons, "no_attachment_pages")
        if download_incomplete or (bool(attachment_paths) and not fully):
            _append_reason(reasons, "unread_attachments")
        return MultimodalOutcome(
            questions=_uncertain(question_numbers, "no_attachment_pages"),
            reasons=reasons,
            real_success=False,
        )
    if download_incomplete or not fully:
        _append_reason(reasons, "unread_attachments")

    try:
        vision = grade_images(
            api_key=api_key,
            question_numbers=question_numbers,
            answer_text=answer_text,
            image_paths=images,
            student_id=student_id,
            attachment_names=[Path(p).name for p in attachment_paths],
            attachments_fully_read=fully and not download_incomplete,
            settings=settings,
        )
    except ModelParseError:
        logger.warning("model JSON parse failed student=%s", student_id)
        _append_reason(reasons, "model_parse_error")
        _append_reason(reasons, "format_invalid")
        return MultimodalOutcome(
            questions=_uncertain(question_numbers, "model_parse_error"),
            reasons=reasons,
            real_success=False,
        )
    except Exception as exc:
        logger.warning(
            "model call failed student=%s err_type=%s",
            student_id,
            type(exc).__name__,
        )
        failed = _stub_outcome(
            question_numbers=question_numbers,
            answer_text=answer_text,
            attachment_paths=attachment_paths,
            student_id=student_id,
            extra=["model_call_failed"],
        )
        for token in reasons:
            _append_reason(failed.reasons, token)
        return failed

    for token in vision.review_reasons:
        _append_reason(reasons, token)
    _apply_model_flags(reasons, vision.flags)
    if _confidence_below_floor(vision, float(settings.model_confidence_floor)):
        _append_reason(reasons, "low_confidence")
    return MultimodalOutcome(
        questions=vision.questions,
        reasons=reasons,
        comment_draft=vision.comment_draft,
        model_needs_review=bool(vision.needs_review),
        overall_confidence=vision.overall_confidence,
        flags=dict(vision.flags),
        real_success=True,
    )


class GradingAgent:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def grade_student(
        self,
        student: StudentResult,
        *,
        question_numbers: list[int],
        answer_source: AnswerSource,
    ) -> StudentResult:
        submitted = bool(
            student.submission.submitted_at
            or student.submission.attachments
            or (student.submission.workflow_state or "")
            in ("submitted", "graded", "pending_review")
        )
        # Explicit unsubmitted
        if (student.submission.workflow_state == "unsubmitted") and not student.submission.attachments:
            submitted = False

        if not submitted:
            outcome = evaluate_questions([], submitted=False)
            student.questions = []
            student.proposed_score = None
            student.proposed_comment = ""
            student.review_reasons = outcome.review_reasons
            student.status = "skipped"  # type: ignore[assignment]
            student.overall_confidence = 1.0
            student.final_score = None
            student.final_comment = ""
            return student

        paths = [
            a.local_path for a in student.submission.attachments if a.local_path
        ]
        download_incomplete = any(
            not a.local_path for a in student.submission.attachments
        )
        answer_text = answer_source.imported_text

        # Vault (if readable) wins; otherwise env is seeded into an empty vault.
        # Do not call the model when the official answer is missing.
        key = ModelKeyVault(self.settings).get_key()
        if key and answer_text and answer_text.strip():
            graded = multimodal_grade_with_model(
                question_numbers=question_numbers,
                answer_text=answer_text,
                attachment_paths=paths,
                student_id=student.student_id,
                api_key=key,
                settings=self.settings,
                download_incomplete=download_incomplete,
            )
        else:
            questions, extra_reasons = stub_multimodal_grade(
                question_numbers=question_numbers,
                answer_text=answer_text,
                attachment_paths=paths,
                student_id=student.student_id,
            )
            graded = MultimodalOutcome(
                questions=questions,
                reasons=list(extra_reasons),
                real_success=False,
            )
            if download_incomplete:
                _append_reason(graded.reasons, "unread_attachments")

        flags = graded.flags
        outcome = evaluate_questions(
            graded.questions,
            submitted=True,
            unreadable=flags.get("unreadable") is True,
            wrong_chapter=flags.get("wrong_chapter") is True and flags.get("unreadable") is not True,
        )
        if graded.real_success and graded.overall_confidence is not None:
            outcome.overall_confidence = min(
                outcome.overall_confidence, graded.overall_confidence
            )
        draft = (graded.comment_draft or "").strip()
        if graded.real_success and draft:
            base = outcome.proposed_comment or ""
            if draft not in base:
                outcome.proposed_comment = f"{base}\n{draft}".strip() if base else draft

        for reason in graded.reasons:
            if reason not in outcome.review_reasons:
                outcome.review_reasons.append(reason)
        if flags.get("wrong_chapter") and "suspected_wrong_chapter" not in outcome.review_reasons:
            outcome.review_reasons.append("suspected_wrong_chapter")
        if _STUB_TOKENS.intersection(graded.reasons) and "stub_multimodal" not in outcome.review_reasons:
            outcome.review_reasons.append("stub_multimodal")

        # Stub tokens still force review. A real parse does not, unless §5 says so.
        must_clear = bool(_CLEAR_SCORE_TOKENS.intersection(outcome.review_reasons)) or (
            graded.model_needs_review
        )
        if must_clear or outcome.status == "needs_review":
            outcome.status = "needs_review"
            outcome.proposed_score = None
        if _STUB_TOKENS.intersection(outcome.review_reasons):
            outcome.proposed_comment = outcome.proposed_comment or _STUB_COMMENT

        student.questions = [
            QuestionItem(
                number=q.number,
                status=q.status,  # type: ignore[arg-type]
                evidence=q.evidence,
                confidence=q.confidence,
            )
            for q in graded.questions
        ]
        student.proposed_score = outcome.proposed_score
        student.proposed_comment = outcome.proposed_comment
        student.review_reasons = outcome.review_reasons
        student.overall_confidence = outcome.overall_confidence
        student.status = outcome.status  # type: ignore[assignment]
        # Default final = proposed for TA edit
        if outcome.status == "proposed":
            student.final_score = outcome.proposed_score
            student.final_comment = outcome.proposed_comment
        else:
            student.final_score = None
            student.final_comment = outcome.proposed_comment or ""
        return student

    def grade_run_students(
        self,
        students: list[StudentResult],
        *,
        question_numbers: list[int],
        answer_source: AnswerSource,
    ) -> list[StudentResult]:
        out = []
        for s in students:
            out.append(
                self.grade_student(
                    s,
                    question_numbers=question_numbers,
                    answer_source=answer_source,
                )
            )
        # Sort: high confidence proposed first (product lock: confidence only sorts)
        def sort_key(st: StudentResult) -> tuple:
            conf = st.overall_confidence or 0.0
            needs = 1 if st.status == "needs_review" else 0
            skipped = 1 if st.status == "skipped" else 0
            return (skipped, needs, -conf)

        out.sort(key=sort_key)
        return out


def approved_result_hash(student: StudentResult, rules_version: str = RULES_VERSION) -> str:
    score = student.effective_score()
    comment = student.effective_comment()
    fp = student.submission.fingerprint or ""
    raw = f"{student.student_id}|{fp}|{rules_version}|{score}|{comment}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]
