"""Scoring rules aligned with docs/02 + docs/04 fixtures."""
from app.scoring import (
    evaluate_questions,
    questions_from_incorrect_list,
    score_from_incorrect,
    unique_incorrect_big_questions,
)

QNUMS = [1, 2, 6, 7, 8, 10, 11, 12, 15]


def test_all_correct_100():
    qs = questions_from_incorrect_list(QNUMS, [])
    out = evaluate_questions(qs)
    assert out.status == "proposed"
    assert out.proposed_score == 100
    assert "全部正确" in out.proposed_comment


def test_duplicate_incorrect_dedupe_98():
    # docs/04 #2: errors 6,6,7 → unique big Q → 98
    qs = questions_from_incorrect_list(QNUMS, [6, 6, 7])
    # list helper already unique; simulate raw with duplicates:
    from app.scoring import QuestionResult

    raw = [
        QuestionResult(6, "incorrect"),
        QuestionResult(6, "incorrect"),
        QuestionResult(7, "incorrect"),
    ] + [QuestionResult(n, "correct") for n in QNUMS if n not in (6, 7)]
    assert unique_incorrect_big_questions(raw) == [6, 7]
    out = evaluate_questions(raw)
    assert out.proposed_score == 98


def test_seven_wrong_floor_95():
    wrong = QNUMS[:7]
    out = evaluate_questions(questions_from_incorrect_list(QNUMS, wrong))
    assert out.proposed_score == 95
    assert score_from_incorrect(wrong) == 95


def test_too_many_blank_needs_review():
    missing = [1, 2, 6, 7]  # 4 >= threshold
    out = evaluate_questions(
        questions_from_incorrect_list(QNUMS, [], missing=missing)
    )
    assert out.status == "needs_review"
    assert out.proposed_score is None
    assert "too_many_blank" in out.review_reasons


def test_unrecognizable_needs_review():
    out = evaluate_questions(
        questions_from_incorrect_list(QNUMS, [], uncertain=[6])
    )
    assert out.status == "needs_review"
    assert out.proposed_score is None
    assert "unrecognizable" in out.review_reasons


def test_not_submitted_skipped():
    out = evaluate_questions([], submitted=False)
    assert out.status == "skipped"
    assert out.proposed_score is None
    assert "not_submitted" in out.review_reasons


def test_few_missing_needs_review():
    out = evaluate_questions(
        questions_from_incorrect_list(QNUMS, [], missing=[15])
    )
    assert out.status == "needs_review"
    assert out.proposed_score is None
    assert any("few_missing" in r for r in out.review_reasons)


def test_unreadable_and_wrong_chapter():
    assert evaluate_questions([], unreadable=True).status == "needs_review"
    assert evaluate_questions([], wrong_chapter=True).status == "needs_review"
