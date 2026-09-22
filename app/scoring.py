"""Course scoring rules aligned with docs/02 and docs/04 fixtures."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable


RULES_VERSION = "100-minus-unique-big-questions-floor95-v1"
FULL_SCORE = 100
FLOOR_SCORE = 95
PENALTY_PER_BIG_QUESTION = 1

# Threshold: "too many blank" → needs_review (no auto low score)
TOO_MANY_BLANK_THRESHOLD = 4
# Few missing without explicit policy → needs_review
FEW_MISSING_NEEDS_REVIEW = True

QuestionStatus = str  # correct|incorrect|missing|uncertain


@dataclass
class QuestionResult:
    number: int
    status: QuestionStatus
    evidence: str = ""
    confidence: float = 1.0


@dataclass
class ScoreOutcome:
    proposed_score: int | None
    proposed_comment: str
    review_reasons: list[str] = field(default_factory=list)
    status: str = "proposed"  # proposed|needs_review|skipped
    unique_incorrect: list[int] = field(default_factory=list)
    overall_confidence: float = 1.0


def unique_incorrect_big_questions(questions: Iterable[QuestionResult | dict]) -> list[int]:
    """Deduplicate incorrect big-question numbers (docs/04 #2)."""
    seen: set[int] = set()
    ordered: list[int] = []
    for q in questions:
        if isinstance(q, dict):
            num, status = int(q["number"]), q["status"]
        else:
            num, status = q.number, q.status
        if status == "incorrect" and num not in seen:
            seen.add(num)
            ordered.append(num)
    return ordered


def score_from_incorrect(unique_incorrect: list[int]) -> int:
    """score = max(95, 100 - n) for unique incorrect big questions."""
    n = len(unique_incorrect)
    return max(FLOOR_SCORE, FULL_SCORE - n * PENALTY_PER_BIG_QUESTION)


def build_comment(unique_incorrect: list[int], extra: str = "") -> str:
    if not unique_incorrect:
        base = "全部正确。"
    else:
        nums = "、".join(str(n) for n in unique_incorrect)
        base = f"扣分题号：{nums}"
    if extra:
        return f"{base} {extra}".strip()
    return base


def evaluate_questions(
    questions: list[QuestionResult] | list[dict[str, Any]],
    *,
    submitted: bool = True,
    unreadable: bool = False,
    wrong_chapter: bool = False,
) -> ScoreOutcome:
    """
    Apply course policy:
    - Not submitted → skipped, no score
    - Unreadable / wrong chapter → needs_review, no auto zero
    - Too many blank / uncertain → needs_review, no auto <95
    - Few missing (policy unclear) → needs_review
    - Else: unique incorrect big Q → max(95, 100-n)
    """
    if not submitted:
        return ScoreOutcome(
            proposed_score=None,
            proposed_comment="",
            review_reasons=["not_submitted"],
            status="skipped",
            overall_confidence=1.0,
        )

    if unreadable:
        return ScoreOutcome(
            proposed_score=None,
            proposed_comment="",
            review_reasons=["unreadable"],
            status="needs_review",
            overall_confidence=0.0,
        )

    if wrong_chapter:
        return ScoreOutcome(
            proposed_score=None,
            proposed_comment="",
            review_reasons=["suspected_wrong_chapter"],
            status="needs_review",
            overall_confidence=0.2,
        )

    normalized: list[QuestionResult] = []
    for q in questions:
        if isinstance(q, dict):
            normalized.append(
                QuestionResult(
                    number=int(q["number"]),
                    status=q["status"],
                    evidence=q.get("evidence", ""),
                    confidence=float(q.get("confidence", 1.0)),
                )
            )
        else:
            normalized.append(q)

    missing = [q.number for q in normalized if q.status == "missing"]
    uncertain = [q.number for q in normalized if q.status == "uncertain"]
    incorrect = unique_incorrect_big_questions(normalized)
    confidences = [q.confidence for q in normalized] or [1.0]
    overall_conf = sum(confidences) / len(confidences)

    reasons: list[str] = []

    if len(missing) >= TOO_MANY_BLANK_THRESHOLD:
        reasons.append("too_many_blank")
    if uncertain:
        reasons.append("unrecognizable")
    if missing and len(missing) < TOO_MANY_BLANK_THRESHOLD and FEW_MISSING_NEEDS_REVIEW:
        # docs/02: few missing scoring not separately specified → needs_review
        reasons.append("few_missing_policy_unclear")
        reasons.append(f"missing_numbers:{','.join(map(str, missing))}")

    if reasons:
        # Do not auto-assign <95 or mechanically floor
        return ScoreOutcome(
            proposed_score=None,
            proposed_comment="",
            review_reasons=reasons,
            status="needs_review",
            unique_incorrect=incorrect,
            overall_confidence=min(overall_conf, 0.5),
        )

    score = score_from_incorrect(incorrect)
    comment = build_comment(incorrect)
    return ScoreOutcome(
        proposed_score=score,
        proposed_comment=comment,
        review_reasons=[],
        status="proposed",
        unique_incorrect=incorrect,
        overall_confidence=overall_conf,
    )


def questions_from_incorrect_list(
    question_numbers: list[int],
    incorrect: list[int],
    missing: list[int] | None = None,
    uncertain: list[int] | None = None,
) -> list[QuestionResult]:
    """Helper for fixtures / deterministic agent."""
    missing = missing or []
    uncertain = uncertain or []
    out: list[QuestionResult] = []
    for n in question_numbers:
        if n in uncertain:
            status = "uncertain"
        elif n in missing:
            status = "missing"
        elif n in incorrect:
            status = "incorrect"
        else:
            status = "correct"
        out.append(QuestionResult(number=n, status=status, evidence=f"q{n}:{status}"))
    return out
