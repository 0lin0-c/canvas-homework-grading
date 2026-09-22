"""Deterministic checks for student-visible comments and placeholder answers.

Pure functions: no network, disk, or model calls. Scores stay in scoring.py.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

ANSWER_MIN_CHARS = 20
COMMENT_MAX_CHARS = 800
PER_QUESTION_MAX_CHARS = 120

# Substring / regex bans. 略 is matched only as its own slot, not inside 策略.
FORBIDDEN_PATTERN_SOURCES: tuple[str, ...] = (
    r"参考答案缺失",
    r"答案库暂无",
    r"答案库没有",
    r"官方答案未导入",
    r"needs_review",
    r"model_call_failed",
    r"model_parse_error",
    r"stub_multimodal",
    r"model_hook_unwired",
    r"\bstub\b",
    r"TODO",
    r"FIXME",
    r"placeholder",
    r"【待复核】未接入",
    r"请助教人工评分与评语",
    r"内部话术",
    r"(?m)(?:^|[：:])\s*略\s*[。．.]?\s*$",
)

FORBIDDEN_PATTERNS: list[re.Pattern[str]] = [
    re.compile(source, re.IGNORECASE) for source in FORBIDDEN_PATTERN_SOURCES
]

_PLACEHOLDER_EXACT = re.compile(
    r"(?i)^(略|暂无|待补充|todo|tbd|placeholder|stub)[\s。．.]*$"
)
_PLACEHOLDER_TOKENS = {
    "略",
    "暂无",
    "待补充",
    "todo",
    "tbd",
    "placeholder",
    "stub",
}
_FW_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")
_ANSWER_SECTION = re.compile(
    r"(?m)^\s*(?:第\s*([0-9０-９]{1,3})\s*题\s*[：:．.]?|([0-9０-９]{1,3})\s*[、.．])\s*"
)
_COMMENT_Q = re.compile(r"第\s*([0-9０-９]{1,3})\s*题\s*[：:]")
_POINT_HINT = re.compile(r"正确|应为|答案是|结果为|要点")
_DIRECTION = re.compile(r"改正|应为|错在|少写|符号")
_SCORE_TALK = re.compile(
    r"(得分|分数|扣\s*\d+\s*分|拟给|suggested\s*score)",
    re.IGNORECASE,
)
_SARCASM = (
    "这么简单都不会",
    "这么简单也不会",
    "什么都不会",
    "笨死了",
    "太蠢了",
    "垃圾答案",
)
_PII_PHRASE = re.compile(r"另一位同学.{0,24}?\d{4,}|学号\s*[:：]?\s*\d{4,}")
_TOKEN_SPLIT = re.compile(r"[\s，,。．.；;：:、/|]+")


@dataclass
class CommentValidationResult:
    ok: bool
    rule_ids: list[str]
    messages: list[str]


def _parse_qnum(raw: str | None) -> int | None:
    if raw is None:
        return None
    try:
        return int(str(raw).translate(_FW_DIGITS))
    except (TypeError, ValueError):
        return None


def _iter_questions(questions: Iterable[Any] | None) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    for item in questions or []:
        if isinstance(item, dict):
            number = _parse_qnum(str(item.get("number")))
            status = str(item.get("status") or "").strip().lower()
        else:
            number = _parse_qnum(str(getattr(item, "number", "")))
            status = str(getattr(item, "status", "") or "").strip().lower()
        if number is None:
            continue
        out.append((number, status))
    return out


def iter_answer_sections(text: str) -> list[tuple[int, str]]:
    """Split official answer text on leading 第N题 / N. / N、 headers."""
    matches = list(_ANSWER_SECTION.finditer(text or ""))
    sections: list[tuple[int, str]] = []
    for index, match in enumerate(matches):
        number = _parse_qnum(match.group(1) or match.group(2))
        if number is None:
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections.append((number, text[match.end() : end].strip()))
    return sections


def split_answer_by_question(
    answer_text: str | None,
    question_numbers: list[int] | None = None,
) -> dict[str, str]:
    found: dict[int, str] = {}
    for number, body in iter_answer_sections(answer_text or ""):
        if number in found:
            found[number] = f"{found[number]}\n{body}".strip()
        else:
            found[number] = body
    if question_numbers:
        return {str(number): found[number] for number in question_numbers if number in found}
    return {str(number): body for number, body in found.items()}


def _is_lue_body(body: str) -> bool:
    compact = re.sub(r"\s+", "", body or "")
    compact = re.sub(r"[。．.]+$", "", compact)
    return compact == "略"


def _only_placeholder_tokens(stripped: str) -> bool:
    tokens = [part for part in _TOKEN_SPLIT.split(stripped) if part]
    if not tokens:
        return True
    return all(part.lower() in _PLACEHOLDER_TOKENS for part in tokens)


def _lue_ratio(text: str, question_numbers: list[int] | None) -> bool:
    sections = iter_answer_sections(text)
    if not sections:
        return False
    lue_numbers = {number for number, body in sections if _is_lue_body(body)}
    if question_numbers:
        denom = len(question_numbers)
        if denom <= 0:
            return False
        hits = sum(1 for number in question_numbers if number in lue_numbers)
        return hits / denom >= 0.5
    hits = sum(1 for _, body in sections if _is_lue_body(body))
    return hits / len(sections) >= 0.5


def is_placeholder_answer(
    text: str | None,
    question_numbers: list[int] | None = None,
) -> bool:
    """True when the official answer is empty, too short, or a stub placeholder."""
    if text is None:
        return True
    stripped = str(text).strip()
    if not stripped or len(stripped) < ANSWER_MIN_CHARS:
        return True
    if _PLACEHOLDER_EXACT.match(stripped):
        return True
    if _only_placeholder_tokens(stripped):
        return True
    if _lue_ratio(stripped, question_numbers):
        return True
    return False


def _comment_segments(comment: str) -> dict[int, list[tuple[str, str]]]:
    matches = list(_COMMENT_Q.finditer(comment or ""))
    grouped: dict[int, list[tuple[str, str]]] = {}
    for index, match in enumerate(matches):
        number = _parse_qnum(match.group(1))
        if number is None:
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(comment)
        full = comment[match.start() : end].strip()
        body = comment[match.end() : end].strip()
        grouped.setdefault(number, []).append((full, body))
    return grouped


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def _ngrams(text: str, size: int) -> set[str]:
    compact = _compact(text)
    if len(compact) < size:
        return set()
    return {compact[i : i + size] for i in range(len(compact) - size + 1)}


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _official_for(number: int, answer_by_question: dict[Any, Any] | None) -> str:
    if not answer_by_question:
        return ""
    if str(number) in answer_by_question:
        return str(answer_by_question[str(number)] or "")
    if number in answer_by_question:
        return str(answer_by_question[number] or "")
    return ""


def _passes_correct_point(full_seg: str, body: str, official: str) -> bool:
    """Recall-leaning check: hint words, answer overlap, or direction + keyword."""
    if _POINT_HINT.search(full_seg) and len(full_seg.strip()) >= 8:
        return True
    official = official or ""
    if not official.strip() or not body.strip():
        return False
    if _ngrams(body, 4) & _ngrams(official, 4):
        return True
    if _jaccard(_ngrams(body, 2), _ngrams(official, 2)) >= 0.15:
        return True
    tokens = [
        token
        for token in re.split(r"[^\u4e00-\u9fffA-Za-z0-9]+", official)
        if len(token) >= 2
    ]
    if _DIRECTION.search(full_seg) and any(token in body for token in tokens):
        return True
    return False


def _other_student_hit(comment: str, other_student_ids: Iterable[str] | None) -> bool:
    if _PII_PHRASE.search(comment or ""):
        return True
    for raw in other_student_ids or []:
        sid = str(raw).strip()
        if len(sid) < 5 or not sid.isdigit():
            continue
        if re.search(rf"(?<!\d){re.escape(sid)}(?!\d)", comment or ""):
            return True
    return False


def validate_student_comment(
    comment: str | None,
    *,
    questions: Iterable[Any] | None = None,
    answer_by_question: dict[Any, Any] | None = None,
    other_student_ids: Iterable[str] | None = None,
) -> CommentValidationResult:
    """Validate one student-visible comment. Does not assign or change a score."""
    raw = "" if comment is None else str(comment)
    rule_ids: list[str] = []
    messages: list[str] = []

    def add(rule_id: str, message: str) -> None:
        if rule_id not in rule_ids:
            rule_ids.append(rule_id)
            messages.append(message)

    for pattern in FORBIDDEN_PATTERNS:
        if pattern.search(raw):
            add("R_FORBIDDEN_LEXICON", "评语含内部或禁止话术，请改成给学生看的表述。")
            break
    if _SCORE_TALK.search(raw):
        add("R_NO_SCORE_TALK", "评语不要写分数、得分或扣分。")
    for phrase in _SARCASM:
        if phrase in raw:
            add("R_SARCASM_LIGHT", "评语含嘲讽表述，请改为中性的改正说明。")
            break
    if _other_student_hit(raw, other_student_ids):
        add("R_PII_CROSS", "评语不要提及其他同学或学号。")
    if len(raw) > COMMENT_MAX_CHARS:
        add("R_TOTAL_LEN", f"评语超过 {COMMENT_MAX_CHARS} 字。")

    segments = _comment_segments(raw)
    long_numbers = [
        number
        for number, parts in segments.items()
        if any(len(body) > PER_QUESTION_MAX_CHARS for _, body in parts)
    ]
    if long_numbers:
        joined = "、".join(f"第{number}题" for number in long_numbers)
        add("R_PER_Q_LEN", f"{joined}的评语超过 {PER_QUESTION_MAX_CHARS} 字。")

    incorrect = [number for number, status in _iter_questions(questions) if status == "incorrect"]
    missing_format: list[int] = []
    missing_points: list[int] = []
    for number in incorrect:
        parts = segments.get(number) or []
        if not parts:
            missing_format.append(number)
            missing_points.append(number)
            continue
        full = "\n".join(part[0] for part in parts)
        body = "\n".join(part[1] for part in parts)
        official = _official_for(number, answer_by_question)
        if not _passes_correct_point(full, body, official):
            missing_points.append(number)
    if missing_format:
        joined = "、".join(f"第{number}题" for number in missing_format)
        add("R_FORMAT_WHEN_INCORRECT", f"错题评语需包含{joined}。")
    if missing_points:
        joined = "、".join(f"第{number}题" for number in missing_points)
        add("R_CORRECT_POINT_HEURISTIC", f"{joined}需要改正方向和正确要点。")

    return CommentValidationResult(ok=not rule_ids, rule_ids=rule_ids, messages=messages)
