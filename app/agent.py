"""Agent grading worker: deterministic rule engine + stub multimodal hook."""
from __future__ import annotations

import hashlib
import logging
import re
from typing import Any

from app.config import Settings, get_settings
from app.models import AnswerSource, QuestionItem, StudentResult
from app.scoring import (
    RULES_VERSION,
    QuestionResult,
    evaluate_questions,
    questions_from_incorrect_list,
)

logger = logging.getLogger(__name__)


def stub_multimodal_grade(
    *,
    question_numbers: list[int],
    answer_text: str | None,
    attachment_paths: list[str],
    student_id: str,
) -> tuple[list[QuestionResult], list[str]]:
    """
    Stub multimodal hook when no model API key is present.

    Heuristics (deterministic, for local/dev):
    - No attachments → unreadable path handled by caller
    - No official answers imported → all uncertain → needs_review
    - If answer_text contains lines like "WRONG:6,7" for this student (test harness)
      or global "INCORRECT:6,7" → mark those incorrect
    - Else mark all correct with low-medium confidence and flag stub_mode
    """
    reasons: list[str] = []
    if not answer_text or not answer_text.strip():
        qs = [
            QuestionResult(
                number=n,
                status="uncertain",
                evidence="no_official_answer_imported",
                confidence=0.1,
            )
            for n in question_numbers
        ]
        return qs, ["no_official_answer", "stub_multimodal"]

    incorrect: list[int] = []
    missing: list[int] = []
    uncertain: list[int] = []

    # Test harness directives in answer paste
    m = re.search(r"INCORRECT\s*:\s*([\d,\s]+)", answer_text, re.I)
    if m:
        incorrect = [int(x.strip()) for x in m.group(1).split(",") if x.strip().isdigit()]

    m = re.search(r"MISSING\s*:\s*([\d,\s]+)", answer_text, re.I)
    if m:
        missing = [int(x.strip()) for x in m.group(1).split(",") if x.strip().isdigit()]

    m = re.search(r"UNCERTAIN\s*:\s*([\d,\s]+)", answer_text, re.I)
    if m:
        uncertain = [int(x.strip()) for x in m.group(1).split(",") if x.strip().isdigit()]

    # Per-student override: STUDENT <id> INCORRECT: ...
    for line in answer_text.splitlines():
        if student_id and student_id in line and re.search(r"INCORRECT\s*:", line, re.I):
            m2 = re.search(r"INCORRECT\s*:\s*([\d,\s]+)", line, re.I)
            if m2:
                incorrect = [
                    int(x.strip()) for x in m2.group(1).split(",") if x.strip().isdigit()
                ]

    if not attachment_paths:
        uncertain = list(question_numbers)
        reasons.append("no_attachment_pages")

    qs = questions_from_incorrect_list(
        question_numbers, incorrect, missing=missing, uncertain=uncertain
    )
    # Lower confidence in stub mode
    for q in qs:
        q.confidence = min(q.confidence, 0.55)
        q.evidence = f"stub:{q.evidence}"
    reasons.append("stub_multimodal")
    return qs, reasons


def multimodal_grade_with_model(
    *,
    question_numbers: list[int],
    answer_text: str | None,
    attachment_paths: list[str],
    student_id: str,
    api_key: str,
) -> tuple[list[QuestionResult], list[str]]:
    """
    Placeholder for real multimodal API.
    Falls back to stub if not implemented / key unused.
    """
    # V1: no hard dependency on a specific provider; mark needs_review if we
    # cannot call a model, but keep schema identical.
    logger.info(
        "Model key present but provider not wired; using stub for student=%s",
        student_id,
    )
    qs, reasons = stub_multimodal_grade(
        question_numbers=question_numbers,
        answer_text=answer_text,
        attachment_paths=attachment_paths,
        student_id=student_id,
    )
    reasons = list(reasons) + ["model_hook_unwired"]
    return qs, reasons


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
        answer_text = answer_source.imported_text

        if self.settings.has_model_key:
            key = self.settings.model_api_key or self.settings.openai_api_key or ""
            qresults, extra_reasons = multimodal_grade_with_model(
                question_numbers=question_numbers,
                answer_text=answer_text,
                attachment_paths=paths,
                student_id=student.student_id,
                api_key=key,
            )
        else:
            qresults, extra_reasons = stub_multimodal_grade(
                question_numbers=question_numbers,
                answer_text=answer_text,
                attachment_paths=paths,
                student_id=student.student_id,
            )

        outcome = evaluate_questions(qresults, submitted=True)
        # Merge stub reasons that force review when no answers
        if "no_official_answer" in extra_reasons and outcome.status == "proposed":
            outcome.status = "needs_review"
            outcome.proposed_score = None
            outcome.review_reasons = list(outcome.review_reasons) + ["no_official_answer"]

        # Always note stub if used (does not alone block writeback if score exists
        # and TA confirms — but high confidence sort uses overall_confidence)
        for r in extra_reasons:
            if r not in outcome.review_reasons and r in (
                "no_official_answer",
                "no_attachment_pages",
            ):
                if outcome.status != "needs_review" and r == "no_official_answer":
                    outcome.status = "needs_review"
                    outcome.proposed_score = None
                if r not in outcome.review_reasons:
                    outcome.review_reasons.append(r)

        student.questions = [
            QuestionItem(
                number=q.number,
                status=q.status,  # type: ignore[arg-type]
                evidence=q.evidence,
                confidence=q.confidence,
            )
            for q in qresults
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
