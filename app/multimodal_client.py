"""DashScope / OpenAI-compatible vision client for per-question status JSON.

The model must not decide the score. Any score field in the payload is ignored.
Do not log API keys, Authorization headers, or image base64.
"""
from __future__ import annotations

import base64
import io
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.config import Settings, get_settings
from app.scoring import QuestionResult

logger = logging.getLogger(__name__)

# SDK loggers can echo request failures that include credential material.
logging.getLogger("openai").setLevel(logging.CRITICAL)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

ALLOWED_STATUS = {"correct", "incorrect", "missing", "uncertain"}
# These tokens are assigned only by the server, never trusted from the model.
SERVER_ONLY_REASONS = {"stub_multimodal", "model_hook_unwired", "model_call_failed"}
REQUIRED_TOP = (
    "questions",
    "comment_draft",
    "overall_confidence",
    "needs_review",
    "review_reasons",
)
REQUIRED_QUESTION = ("number", "status", "confidence", "evidence")

SYSTEM_PROMPT = """你是课程作业助教的多模态批改助手。对照官方答案，只判断给定大题列表里每一题的状态。
硬性规则：
- 禁止计算或输出最终分数。不要输出 score、proposed_score、grade 字段；若输出也会被丢弃。
- status 只能是 correct、incorrect、missing、uncertain。
- 没写该题用 missing；看不清或无法判断用 uncertain；明确做错用 incorrect；与官方答案一致或等价正确用 correct。
- 必须覆盖题号列表中的每一题，不要增加列表外的题。
- 只输出一个 JSON 对象，不要 Markdown。
JSON 形状：
{
  "questions": [{"number": 1, "status": "correct", "confidence": 0.0, "evidence": "页码或关键步骤"}],
  "comment_draft": "给学生可见的简短评语草稿",
  "overall_confidence": 0.0,
  "needs_review": false,
  "review_reasons": [],
  "flags": {
    "too_many_blank": false,
    "unreadable": false,
    "wrong_chapter": false,
    "attachments_fully_read": true
  }
}
review_reasons 只用这些标记：low_confidence、unread_attachments、too_many_blank、unreadable、unrecognizable、suspected_wrong_chapter、few_missing_policy_unclear。
"""


class ModelCallError(Exception):
    """HTTP/SDK/timeout/empty response. Message is a short error type, never a key."""


class ModelParseError(Exception):
    """Response was not the required JSON schema."""


@dataclass
class VisionGrade:
    questions: list[QuestionResult]
    comment_draft: str
    overall_confidence: float
    needs_review: bool
    review_reasons: list[str] = field(default_factory=list)
    flags: dict[str, Any] = field(default_factory=dict)


def _extract_json_object(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    if not text:
        raise ModelParseError("empty")
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise ModelParseError("json") from None
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            raise ModelParseError("json") from None
    if not isinstance(data, dict):
        raise ModelParseError("not_object")
    return data


def _as_bool(value: Any, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    raise ModelParseError(field_name)


def _reason_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        raise ModelParseError("review_reasons")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ModelParseError("review_reasons")
        token = item.strip()
        if not token or token in SERVER_ONLY_REASONS:
            continue
        if token not in out:
            out.append(token)
    return out


def parse_model_payload(raw: str, question_numbers: list[int]) -> VisionGrade:
    """Validate the model JSON. Score-like fields are not read."""
    data = _extract_json_object(raw)
    for key in REQUIRED_TOP:
        if key not in data:
            raise ModelParseError(f"missing_{key}")
    questions_raw = data["questions"]
    if not isinstance(questions_raw, list):
        raise ModelParseError("questions")
    by_num: dict[int, QuestionResult] = {}
    for item in questions_raw:
        if not isinstance(item, dict):
            raise ModelParseError("question_item")
        for key in REQUIRED_QUESTION:
            if key not in item:
                raise ModelParseError(f"question_missing_{key}")
        try:
            number = int(item["number"])
        except (TypeError, ValueError):
            raise ModelParseError("question_number") from None
        if number in by_num:
            raise ModelParseError("duplicate_question")
        status = str(item["status"]).strip().lower()
        if status not in ALLOWED_STATUS:
            raise ModelParseError("question_status")
        try:
            confidence = float(item["confidence"])
        except (TypeError, ValueError):
            raise ModelParseError("question_confidence") from None
        if confidence < 0 or confidence > 1:
            raise ModelParseError("question_confidence_range")
        by_num[number] = QuestionResult(
            number=number,
            status=status,
            evidence=str(item.get("evidence") or "")[:500],
            confidence=confidence,
        )
    expected = list(question_numbers)
    if set(by_num) != set(expected) or len(by_num) != len(expected):
        raise ModelParseError("question_set_mismatch")
    try:
        overall = float(data["overall_confidence"])
    except (TypeError, ValueError):
        raise ModelParseError("overall_confidence") from None
    if overall < 0 or overall > 1:
        raise ModelParseError("overall_confidence_range")
    comment = data["comment_draft"]
    if comment is None:
        comment = ""
    if not isinstance(comment, str):
        raise ModelParseError("comment_draft")
    flags = data.get("flags") or {}
    if not isinstance(flags, dict):
        raise ModelParseError("flags")
    return VisionGrade(
        questions=[by_num[n] for n in expected],
        comment_draft=comment.strip(),
        overall_confidence=overall,
        needs_review=_as_bool(data["needs_review"], "needs_review"),
        review_reasons=_reason_list(data["review_reasons"]),
        flags=flags,
    )


def image_to_data_url(path: Path, *, max_edge: int = 1600) -> str:
    """JPEG data URL. Downscale so a multi-page call stays within a reasonable payload."""
    try:
        from PIL import Image

        with Image.open(path) as im:
            frame = im.convert("RGB")
            width, height = frame.size
            edge = max(width, height)
            if edge > max_edge:
                scale = max_edge / float(edge)
                frame = frame.resize(
                    (max(1, int(width * scale)), max(1, int(height * scale))),
                    Image.Resampling.LANCZOS,
                )
            buf = io.BytesIO()
            frame.save(buf, format="JPEG", quality=80)
    except Exception:
        raise ModelCallError("image_encode_failed") from None
    encoded = base64.standard_b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


def build_user_text(
    *,
    question_numbers: list[int],
    answer_text: str,
    student_id: str,
    filenames: list[str],
    page_count: int,
    attachments_fully_read: bool,
) -> str:
    payload = {
        "student_id": student_id,
        "question_numbers": question_numbers,
        "attachment_filenames": filenames,
        "page_images": page_count,
        "attachments_fully_read": attachments_fully_read,
        "official_answer": answer_text,
    }
    return (
        "请根据随后的页面图片和下面的 JSON 元数据判断各大题。"
        "不要输出分数。\n"
        + json.dumps(payload, ensure_ascii=False)
    )


def chat_completion_text(
    *,
    api_key: str,
    base_url: str,
    model: str,
    messages: list[dict[str, Any]],
    timeout_sec: float,
    client: Any | None = None,
) -> str:
    """One compatible-mode chat completion. Retries once without JSON mode.

    `client` is a test seam. Production builds an OpenAI client pointed at base_url.
    """
    if client is None:
        try:
            from openai import OpenAI
        except Exception:
            raise ModelCallError("openai_sdk_missing") from None
        client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout_sec,
            max_retries=0,
        )
    # DashScope Qwen3.8 vision + response_format=json_object often hangs/timeouts.
    # Prefer prompt-enforced JSON first; optionally retry with json_object for openai.
    provider_hint = (base_url or "").lower()
    if "dashscope" in provider_hint:
        attempts: tuple[dict[str, Any], ...] = ({"temperature": 0},)
    else:
        attempts = (
            {"temperature": 0, "response_format": {"type": "json_object"}},
            {"temperature": 0},
        )
    last_type = "empty_response"
    for extra in attempts:
        try:
            kwargs = {"model": model, "messages": messages, **extra}
            if "dashscope" in (base_url or "").lower():
                # Qwen3.8 defaults to thinking mode which exceeds our grading timeouts.
                eb = dict(kwargs.get("extra_body") or {})
                eb.setdefault("enable_thinking", False)
                kwargs["extra_body"] = eb
            kwargs.setdefault("max_tokens", 2048)
            resp = client.chat.completions.create(**kwargs)
        except Exception as exc:
            last_type = type(exc).__name__
            logger.warning("model request attempt failed err_type=%s", last_type)
            continue
        content = ""
        choices = getattr(resp, "choices", None) or []
        if choices:
            message = getattr(choices[0], "message", None)
            content = getattr(message, "content", None) or ""
        if str(content).strip():
            return str(content)
        last_type = "empty_response"
    raise ModelCallError(last_type)


def grade_images(
    *,
    api_key: str,
    question_numbers: list[int],
    answer_text: str,
    image_paths: list[Path],
    student_id: str,
    attachment_names: list[str],
    attachments_fully_read: bool,
    settings: Settings | None = None,
) -> VisionGrade:
    settings = settings or get_settings()
    provider = (settings.model_provider or "dashscope").strip().lower()
    if provider not in {"dashscope", "openai"}:
        raise ModelCallError("provider_not_supported")
    if not image_paths:
        raise ModelCallError("no_images")
    logger.info(
        "calling model provider=%s model=%s student=%s pages=%s",
        provider,
        settings.model_name,
        student_id,
        len(image_paths),
    )
    parts: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": build_user_text(
                question_numbers=question_numbers,
                answer_text=answer_text,
                student_id=student_id,
                filenames=attachment_names,
                page_count=len(image_paths),
                attachments_fully_read=attachments_fully_read,
            ),
        }
    ]
    for path in image_paths:
        parts.append(
            {
                "type": "image_url",
                "image_url": {"url": image_to_data_url(path)},
            }
        )
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": parts},
    ]
    raw = chat_completion_text(
        api_key=api_key,
        base_url=settings.model_base_url,
        model=settings.model_name,
        messages=messages,
        timeout_sec=settings.model_timeout_sec,
    )
    parsed = parse_model_payload(raw, question_numbers)
    logger.info(
        "model response parsed student=%s questions=%s",
        student_id,
        len(parsed.questions),
    )
    return parsed
