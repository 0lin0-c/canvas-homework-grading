"""Writeback / conflict state machine (docs/04 scenarios 8–14)."""
from app.models import PlatformSnapshot, StudentResult, SubmissionMeta
from app.writeback import WritebackService, can_writeback, grades_equal


def _student(**kwargs) -> StudentResult:
    base = dict(
        student_id="111",
        status="proposed",
        proposed_score=98,
        proposed_comment="扣分题号：6、7",
        final_score=98,
        final_comment="扣分题号：6、7",
        platform_before=PlatformSnapshot(grade=None, comment_evidence=[]),
        submission=SubmissionMeta(submitted_at="2026-09-16T12:00:00Z", fingerprint="abc"),
    )
    base.update(kwargs)
    return StudentResult(**base)


def test_needs_review_blocks_writeback():
    s = _student(status="needs_review", final_score=None, proposed_score=None)
    ok, reason = can_writeback(s)
    assert not ok
    assert reason == "needs_review_blocked"


def test_force_authorized_allows():
    s = _student(status="needs_review", force_authorized=True, final_score=96)
    ok, _ = can_writeback(s)
    assert ok


def test_skipped_cannot_write():
    ok, reason = can_writeback(_student(status="skipped", final_score=None))
    assert not ok
    assert reason == "skipped_not_submitted"


def test_grades_equal():
    assert grades_equal(98, "98")
    assert grades_equal(98.0, 98)
    assert not grades_equal(97, 98)


def test_dry_run_complete_and_duplicate_comment_skip():
    svc = WritebackService()
    s = _student(
        platform_before=PlatformSnapshot(grade=None, comment_evidence=[]),
    )
    r = svc.writeback_student(
        course_id=1,
        assignment_id=2,
        student=s,
        rules_version="v1",
        dry_run=True,
    )
    assert r.ok
    assert s.status == "complete"
    assert "put_grade" in r.steps_done
    assert "put_comment" in r.steps_done or "comment_skip_duplicate" in r.steps_done

    # Second pass: already synced
    s2 = _student(
        platform_before=PlatformSnapshot(
            grade="98", comment_evidence=["扣分题号：6、7"]
        ),
    )
    r2 = svc.writeback_student(
        course_id=1, assignment_id=2, student=s2, rules_version="v1", dry_run=True
    )
    assert r2.ok
    assert "already_synced" in r2.steps_done


def test_conflict_when_platform_grade_changed():
    svc = WritebackService()
    s = _student(
        platform_before=PlatformSnapshot(grade="100", comment_evidence=[]),
        final_score=98,
    )
    # dry_run uses platform_before as live → live grade 100 != before wait
    # Actually dry_run sets live = platform_before, so no conflict with itself.
    # Simulate: before was 100, we want 98 — conflict check compares live to before;
    # if equal to before, proceeds to write. To test conflict, we need live != before
    # and live != target. Use a tiny fake by monkeypatching client.

    class FakeClient:
        def get_submission(self, *a, **k):
            return {"grade": "97", "score": 97, "submission_comments": []}

        def put_grade(self, *a, **k):
            raise AssertionError("should not write on conflict")

        def put_comment(self, *a, **k):
            raise AssertionError("should not write on conflict")

    svc.client = FakeClient()  # type: ignore[assignment]
    r = svc.writeback_student(
        course_id=1, assignment_id=2, student=s, rules_version="v1", dry_run=False
    )
    assert not r.ok
    assert r.status == "conflict"
    assert s.status == "conflict"


def test_batch_gate_max_batch():
    from app.config import Settings

    svc = WritebackService(settings=Settings(writeback_max_batch=1))
    students = [
        _student(student_id="1"),
        _student(student_id="2"),
    ]
    results = svc.writeback_batch(
        course_id=1,
        assignment_id=2,
        students=students,
        student_ids=["1", "2"],
        rules_version="v1",
        confirm=True,
        dry_run=True,
    )
    assert all(not r.ok for r in results)
    assert "WRITEBACK_MAX_BATCH" in (results[0].error or "")


def test_confirm_required():
    svc = WritebackService()
    results = svc.writeback_batch(
        course_id=1,
        assignment_id=2,
        students=[_student()],
        student_ids=["111"],
        rules_version="v1",
        confirm=False,
        dry_run=True,
    )
    assert results[0].error == "confirm_required"


def test_partial_resume_comment_only():
    """docs/04 #10: grade already verified → only comment."""
    svc = WritebackService()
    s = _student(
        status="grade_verified",
        writeback_step="put_comment",
        platform_before=PlatformSnapshot(grade="98", comment_evidence=[]),
    )
    r = svc.writeback_student(
        course_id=1, assignment_id=2, student=s, rules_version="v1", dry_run=True
    )
    assert r.ok
    assert s.status == "complete"
    assert "put_grade" not in r.steps_done
