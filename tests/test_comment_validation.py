"""Pure comment and placeholder rules (no network)."""
import pytest

from app.comment_validation import (
    ANSWER_MIN_CHARS,
    COMMENT_MAX_CHARS,
    PER_QUESTION_MAX_CHARS,
    is_placeholder_answer,
    split_answer_by_question,
    validate_student_comment,
)

_CORRECT = [{"number": 1, "status": "correct"}, {"number": 6, "status": "correct"}]
_WRONG_6 = [{"number": 1, "status": "correct"}, {"number": 6, "status": "incorrect"}]


def _ok(comment, **kwargs):
    result = validate_student_comment(comment, **kwargs)
    assert result.ok, (result.rule_ids, result.messages)
    return result


def _bad(comment, rule_id, **kwargs):
    result = validate_student_comment(comment, **kwargs)
    assert not result.ok
    assert rule_id in result.rule_ids
    assert result.messages
    return result


def test_placeholder_blank_short_and_tokens():
    assert is_placeholder_answer(None) is True
    assert is_placeholder_answer("") is True
    assert is_placeholder_answer("   ") is True
    assert is_placeholder_answer("略") is True
    assert is_placeholder_answer("  略。 ") is True
    assert is_placeholder_answer("暂无") is True
    assert is_placeholder_answer("待补充") is True
    assert is_placeholder_answer("TODO") is True
    assert is_placeholder_answer("TBD") is True
    assert is_placeholder_answer("placeholder") is True
    assert is_placeholder_answer("stub") is True
    assert is_placeholder_answer("TODO: fill later") is True
    assert len("甲" * (ANSWER_MIN_CHARS - 1)) < ANSWER_MIN_CHARS
    assert is_placeholder_answer("甲" * (ANSWER_MIN_CHARS - 1)) is True
    assert is_placeholder_answer("甲" * ANSWER_MIN_CHARS) is False
    long_todo = "TODO " * 6
    assert len(long_todo.strip()) >= ANSWER_MIN_CHARS
    assert is_placeholder_answer(long_todo) is True


def test_placeholder_lue_ratio_uses_question_numbers():
    text = (
        "第1题\n略\n"
        "第2题\n略。\n"
        "第3题\n条件概率是在缩小后的样本空间里计算事件比例。\n"
    )
    assert len(text.strip()) >= ANSWER_MIN_CHARS
    assert is_placeholder_answer(text) is True
    assert is_placeholder_answer(text, question_numbers=[1, 2, 3]) is True
    assert is_placeholder_answer(text, question_numbers=[1, 2, 3, 4, 5]) is False
    real = (
        "第1题\n略\n"
        "第2题\n独立事件的概率等于各自概率相乘，需要写出公式。\n"
        "第3题\n条件概率的定义是样本空间缩小后的比例。\n"
    )
    assert is_placeholder_answer(real) is False


def test_real_answer_is_not_placeholder():
    text = "第6题\nP(A∪B)=P(A)+P(B)-P(A∩B)，并说明互斥时相交项为零。\n"
    assert is_placeholder_answer(text, question_numbers=[6]) is False


def test_split_answer_by_question_headers():
    text = "前言不要进题\n第6题\nP(A∪B)=1/2\n7、条件概率先缩小样本空间\n"
    split = split_answer_by_question(text, [6, 7, 8])
    assert split["6"].startswith("P(A∪B)=1/2")
    assert "缩小样本空间" in split["7"]
    assert "8" not in split
    assert "6" in split_answer_by_question(text, None)


def test_all_correct_short_comment_passes():
    _ok("全部正确。", questions=_CORRECT)
    _ok("全部正确，继续保持。", questions=_CORRECT)


def test_incorrect_with_direction_and_key_point_passes():
    _ok(
        "第6题：符号用反，正确应为 P(A∪B)=P(A)+P(B)-P(A∩B)。",
        questions=_WRONG_6,
    )
    _ok(
        "第６题：符号用反，正确应为42",
        questions=_WRONG_6,
    )


def test_incorrect_without_key_point_fails():
    result = _bad("第6题：错了", "R_CORRECT_POINT_HEURISTIC", questions=_WRONG_6)
    assert "R_FORMAT_WHEN_INCORRECT" not in result.rule_ids
    _bad("符号用反，正确应为42", "R_FORMAT_WHEN_INCORRECT", questions=_WRONG_6)
    _bad(
        "第6题：符号用反，正确应为42",
        "R_FORMAT_WHEN_INCORRECT",
        questions=[
            {"number": 6, "status": "incorrect"},
            {"number": 7, "status": "incorrect"},
        ],
    )


def test_score_only_line_is_not_a_correct_point():
    result = validate_student_comment("扣分题号：6、7", questions=_CORRECT)
    assert result.ok
    assert "R_NO_SCORE_TALK" not in result.rule_ids
    result = validate_student_comment("扣分题号：6", questions=_WRONG_6)
    assert not result.ok
    assert "R_NO_SCORE_TALK" not in result.rule_ids
    assert "R_CORRECT_POINT_HEURISTIC" in result.rule_ids
    _bad("这次得分很高", "R_NO_SCORE_TALK", questions=_CORRECT)
    _bad("扣 6 分", "R_NO_SCORE_TALK", questions=_CORRECT)


@pytest.mark.parametrize(
    "comment",
    [
        "这里有 stub 字样",
        "状态是 needs_review",
        "参考答案缺失",
        "答案库暂无",
        "TODO 以后再写",
        "placeholder",
        "【待复核】未接入真实批改",
        "请助教人工评分与评语",
        "第6题：略",
    ],
)
def test_forbidden_lexicon(comment):
    _bad(comment, "R_FORBIDDEN_LEXICON", questions=_CORRECT)


def test_lue_inside_normal_word_is_allowed():
    _ok("策略略有调整，整体全部正确。", questions=_CORRECT)
    _ok("学生的解答策略清楚，全部正确。", questions=_CORRECT)


def test_stubborn_is_not_the_stub_token():
    _ok("这里提到 stubborn 这个英文词，全部正确。", questions=_CORRECT)


def test_total_length_boundary():
    base = "全部正确。"
    exact = base + ("好" * (COMMENT_MAX_CHARS - len(base)))
    assert len(exact) == COMMENT_MAX_CHARS
    _ok(exact, questions=_CORRECT)
    _bad(exact + "好", "R_TOTAL_LEN", questions=_CORRECT)


def test_per_question_length_boundary():
    hint = "正确应为"
    body = hint + ("步" * (PER_QUESTION_MAX_CHARS - len(hint)))
    assert len(body) == PER_QUESTION_MAX_CHARS
    _ok(f"第6题：{body}", questions=_WRONG_6)
    _bad(f"第6题：{body}步", "R_PER_Q_LEN", questions=_WRONG_6)


def test_ngram_overlap_counts_as_correct_point():
    formula = "P(A∪B)=P(A)+P(B)-P(A∩B)"
    _ok(
        f"第6题：{formula}",
        questions=_WRONG_6,
        answer_by_question={"6": formula},
    )
    missed = validate_student_comment(
        "第6题：错了",
        questions=_WRONG_6,
        answer_by_question={"6": "答案为42"},
    )
    assert not missed.ok
    assert "R_CORRECT_POINT_HEURISTIC" in missed.rule_ids


def test_direction_plus_official_keyword():
    _ok(
        "第6题：符号用反了，应写成独立事件概率相乘",
        questions=_WRONG_6,
        answer_by_question={"6": "独立事件概率相乘"},
    )


def test_sarcasm_and_cross_student():
    _bad("这么简单都不会", "R_SARCASM_LIGHT", questions=_CORRECT)
    _bad(
        "另一位同学 20210001 的做法不同",
        "R_PII_CROSS",
        questions=_CORRECT,
        other_student_ids=["20210001"],
    )
    _ok(
        "第6题：符号用反，正确应为42",
        questions=_WRONG_6,
        other_student_ids=["42"],
    )
