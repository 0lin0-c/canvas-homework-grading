"""Writeback state machine: conflict → grade → verify → comment → verify."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from app.agent import approved_result_hash
from app.canvas_client import CanvasAPIError, CanvasAuthError, CanvasClient
from app.comment_validation import (
    is_placeholder_answer,
    split_answer_by_question,
    validate_student_comment,
)
from app.config import Settings, get_settings
from app.models import PlatformSnapshot, StudentResult, WritebackItemResult

logger = logging.getLogger(__name__)


def _norm_grade(g: Any) -> str | None:
    if g is None or g == "":
        return None
    try:
        return str(float(g))
    except (TypeError, ValueError):
        return str(g).strip()


def grades_equal(a: Any, b: Any) -> bool:
    na, nb = _norm_grade(a), _norm_grade(b)
    if na is None and nb is None:
        return True
    if na is None or nb is None:
        return False
    try:
        return abs(float(na) - float(nb)) < 1e-6
    except ValueError:
        return na == nb


def can_writeback(
    student: StudentResult,
    *,
    require_confirm_fields: bool = True,
    answer_text: str | None = None,
    answer_by_question: dict | None = None,
    question_numbers: list[int] | None = None,
    other_student_ids: list[str] | None = None,
) -> tuple[bool, str]:
    if student.status == "skipped":
        return False, "skipped_not_submitted"
    if student.status == "complete":
        return False, "already_complete"
    if student.status == "needs_review" and not student.force_authorized:
        return False, "needs_review_blocked"
    if student.status == "conflict":
        return False, "conflict"
    score = student.effective_score()
    if score is None:
        return False, "no_final_score"
    # Placeholder keys block writeback even when force_authorized is set.
    if (
        answer_text is not None
        and str(answer_text).strip()
        and is_placeholder_answer(str(answer_text), question_numbers)
    ):
        return False, "comment_validation:R_PLACEHOLDER_ANSWER"
    comment = student.effective_comment()
    if require_confirm_fields and not (comment or "").strip():
        return False, "no_comment"
    # Comment rules are student-visible. force_authorized does not skip them.
    if (comment or "").strip():
        by_question = answer_by_question
        if by_question is None and (answer_text or question_numbers):
            by_question = split_answer_by_question(answer_text, question_numbers or [])
        checked = validate_student_comment(
            comment,
            questions=student.questions,
            answer_by_question=by_question,
            other_student_ids=other_student_ids,
        )
        if not checked.ok:
            rule_id = checked.rule_ids[0] if checked.rule_ids else "invalid"
            return False, f"comment_validation:{rule_id}"
    return True, "ok"


class WritebackService:
    def __init__(
        self,
        client: CanvasClient | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.client = client or CanvasClient(settings=self.settings)

    def writeback_student(
        self,
        *,
        course_id: int,
        assignment_id: int,
        student: StudentResult,
        rules_version: str,
        dry_run: bool = False,
        answer_text: str | None = None,
        answer_by_question: dict | None = None,
        question_numbers: list[int] | None = None,
        other_student_ids: list[str] | None = None,
    ) -> WritebackItemResult:
        """
        Per-student pipeline (docs/03 + impl plan §4.4):
        1. Re-read platform_before
        2. Conflict detection
        3. PUT grade (if needed)
        4. Verify grade
        5. PUT comment (if needed, skip duplicate)
        6. Verify comment
        7. Mark complete
        """
        ok, reason = can_writeback(
            student,
            answer_text=answer_text,
            answer_by_question=answer_by_question,
            question_numbers=question_numbers,
            other_student_ids=other_student_ids,
        )
        if not ok:
            student.writeback_error = reason
            return WritebackItemResult(
                student_id=student.student_id,
                ok=False,
                status=student.status,
                error=reason,
            )

        steps: list[str] = []
        target_score = student.effective_score()
        target_comment = student.effective_comment()
        student.approved_result_hash = approved_result_hash(student, rules_version)

        try:
            # Resume from partial
            resume = student.writeback_step
            if resume in ("idle", "conflict_check", "done"):
                resume = "conflict_check"

            # --- 1+2 conflict check ---
            if student.status != "partial" or student.writeback_step in (
                "idle",
                "conflict_check",
            ):
                student.writeback_step = "conflict_check"
                if dry_run:
                    live = {
                        "grade": student.platform_before.grade,
                        "score": student.platform_before.score,
                        "submission_comments": [
                            {"comment": c} for c in student.platform_before.comment_evidence
                        ],
                    }
                else:
                    live = self.client.get_submission(
                        course_id, assignment_id, student.student_id
                    )
                live_grade = live.get("grade")
                live_score = live.get("score")
                live_comments = [
                    (c.get("comment") or "").strip()
                    for c in (live.get("submission_comments") or [])
                    if (c.get("comment") or "").strip()
                ]

                before_grade = student.platform_before.grade
                before_comments = list(student.platform_before.comment_evidence)

                # Human changed grade since we snapshotted
                if before_grade is not None and not grades_equal(live_grade, before_grade):
                    # Unless live already matches our target (idempotent)
                    if not grades_equal(live_grade, target_score):
                        student.status = "conflict"
                        student.writeback_error = "platform_grade_changed"
                        return WritebackItemResult(
                            student_id=student.student_id,
                            ok=False,
                            status="conflict",
                            error="platform_grade_changed",
                            steps_done=steps,
                        )

                # New comments from others that aren't ours — still allow if grade ok
                student.platform_before = PlatformSnapshot(
                    grade=live_grade,
                    score=live_score,
                    comment_evidence=live_comments,
                    graded_at=live.get("graded_at"),
                )
                steps.append("conflict_check")

                # Already complete identical?
                if grades_equal(live_grade, target_score) and target_comment in live_comments:
                    student.status = "complete"
                    student.writeback_step = "done"
                    student.platform_after = PlatformSnapshot(
                        grade=live_grade,
                        score=live_score,
                        comment_evidence=live_comments,
                    )
                    student.verified_at = datetime.now(timezone.utc).isoformat()
                    return WritebackItemResult(
                        student_id=student.student_id,
                        ok=True,
                        status="complete",
                        steps_done=steps + ["already_synced"],
                    )

            # --- 3+4 grade ---
            need_grade = True
            if student.status == "grade_verified" or student.writeback_step in (
                "put_comment",
                "verify_comment",
            ):
                need_grade = False

            if need_grade:
                student.writeback_step = "put_grade"
                if not dry_run:
                    self.client.put_grade(
                        course_id, assignment_id, student.student_id, target_score  # type: ignore[arg-type]
                    )
                steps.append("put_grade")

                student.writeback_step = "verify_grade"
                if dry_run:
                    verified_grade = target_score
                    verified_score = float(target_score) if target_score is not None else None
                else:
                    live = self.client.get_submission(
                        course_id, assignment_id, student.student_id
                    )
                    verified_grade = live.get("grade")
                    verified_score = live.get("score")
                if not grades_equal(verified_grade, target_score):
                    student.status = "failed"
                    student.writeback_error = "grade_verify_mismatch"
                    student.writeback_step = "verify_grade"
                    return WritebackItemResult(
                        student_id=student.student_id,
                        ok=False,
                        status="failed",
                        error="grade_verify_mismatch",
                        steps_done=steps,
                    )
                student.status = "grade_verified"
                steps.append("verify_grade")

            # --- 5+6 comment ---
            if target_comment:
                # Skip duplicate
                existing = list(student.platform_before.comment_evidence)
                if not dry_run:
                    live = self.client.get_submission(
                        course_id, assignment_id, student.student_id
                    )
                    existing = [
                        (c.get("comment") or "").strip()
                        for c in (live.get("submission_comments") or [])
                        if (c.get("comment") or "").strip()
                    ]

                if target_comment in existing:
                    steps.append("comment_skip_duplicate")
                else:
                    student.writeback_step = "put_comment"
                    if not dry_run:
                        self.client.put_comment(
                            course_id,
                            assignment_id,
                            student.student_id,
                            target_comment,
                        )
                    steps.append("put_comment")

                    student.writeback_step = "verify_comment"
                    if dry_run:
                        comments_after = existing + [target_comment]
                    else:
                        live = self.client.get_submission(
                            course_id, assignment_id, student.student_id
                        )
                        comments_after = [
                            (c.get("comment") or "").strip()
                            for c in (live.get("submission_comments") or [])
                            if (c.get("comment") or "").strip()
                        ]
                    if target_comment not in comments_after:
                        student.status = "partial"
                        student.writeback_error = "comment_verify_missing"
                        return WritebackItemResult(
                            student_id=student.student_id,
                            ok=False,
                            status="partial",
                            error="comment_verify_missing",
                            steps_done=steps,
                        )
                    # Detect accidental duplicate spam
                    if comments_after.count(target_comment) > 1:
                        logger.warning(
                            "Duplicate comment detected for student=%s (not re-posted by us this run)",
                            student.student_id,
                        )
                    existing = comments_after
                    steps.append("verify_comment")

                student.status = "comment_verified"

            # --- 7 complete ---
            student.writeback_step = "done"
            student.status = "complete"
            student.verified_at = datetime.now(timezone.utc).isoformat()
            student.platform_after = PlatformSnapshot(
                grade=_norm_grade(target_score),
                score=float(target_score) if target_score is not None else None,
                comment_evidence=existing if target_comment else list(student.platform_before.comment_evidence),
            )
            student.writeback_error = None
            return WritebackItemResult(
                student_id=student.student_id,
                ok=True,
                status="complete",
                steps_done=steps,
            )

        except CanvasAuthError as exc:
            student.status = "failed"
            student.writeback_error = "auth_failed"
            return WritebackItemResult(
                student_id=student.student_id,
                ok=False,
                status="failed",
                error="auth_failed",
                steps_done=steps,
            )
        except CanvasAPIError as exc:
            student.status = "partial" if "verify_grade" in steps or "put_grade" in steps else "failed"
            student.writeback_error = f"api_{exc.status_code}"
            return WritebackItemResult(
                student_id=student.student_id,
                ok=False,
                status=student.status,
                error=student.writeback_error,
                steps_done=steps,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("writeback unexpected error for %s", student.student_id)
            student.status = "unknown_after_write"
            student.writeback_error = type(exc).__name__
            return WritebackItemResult(
                student_id=student.student_id,
                ok=False,
                status="unknown_after_write",
                error=type(exc).__name__,
                steps_done=steps,
            )

    def writeback_batch(
        self,
        *,
        course_id: int,
        assignment_id: int,
        students: list[StudentResult],
        student_ids: list[str],
        rules_version: str,
        confirm: bool,
        dry_run: bool = False,
        answer_text: str | None = None,
        question_numbers: list[int] | None = None,
    ) -> list[WritebackItemResult]:
        if not confirm:
            return [
                WritebackItemResult(
                    student_id=sid,
                    ok=False,
                    status="rejected",
                    error="confirm_required",
                )
                for sid in student_ids
            ]

        max_batch = max(1, int(self.settings.writeback_max_batch))
        selected = [s for s in students if s.student_id in set(student_ids)]
        if len(selected) > max_batch:
            return [
                WritebackItemResult(
                    student_id=s.student_id,
                    ok=False,
                    status="rejected",
                    error=f"WRITEBACK_MAX_BATCH={max_batch}",
                )
                for s in selected
            ]

        results: list[WritebackItemResult] = []
        by_id = {s.student_id: s for s in students}
        by_question = split_answer_by_question(answer_text, question_numbers or [])
        for sid in student_ids:
            st = by_id.get(sid)
            if not st:
                results.append(
                    WritebackItemResult(
                        student_id=sid, ok=False, status="missing", error="not_found"
                    )
                )
                continue
            others = [s.student_id for s in students if s.student_id != sid]
            results.append(
                self.writeback_student(
                    course_id=course_id,
                    assignment_id=assignment_id,
                    student=st,
                    rules_version=rules_version,
                    dry_run=dry_run,
                    answer_text=answer_text,
                    answer_by_question=by_question,
                    question_numbers=question_numbers,
                    other_student_ids=others,
                )
            )
        return results
