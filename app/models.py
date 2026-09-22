"""Pydantic models aligned with docs/03-run-record.md."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


Mode = Literal["rehearsal", "suggest", "writeback"]
StudentStatus = Literal[
    "pending",
    "read_complete",
    "needs_review",
    "proposed",
    "grade_verified",
    "comment_verified",
    "complete",
    "unknown_after_write",
    "skipped",
    "failed",
    "conflict",
    "partial",
]
QuestionStatus = Literal["correct", "incorrect", "missing", "uncertain"]
WritebackStep = Literal[
    "idle",
    "conflict_check",
    "put_grade",
    "verify_grade",
    "put_comment",
    "verify_comment",
    "done",
]


class AttachmentMeta(BaseModel):
    id: str
    name: str = ""
    url: str | None = None
    size: int | None = None
    page_count: int | None = None
    pages_read: list[int] = Field(default_factory=list)
    local_path: str | None = None


class SubmissionMeta(BaseModel):
    submitted_at: str | None = None
    attempt: int | None = None
    workflow_state: str | None = None
    attachments: list[AttachmentMeta] = Field(default_factory=list)
    fingerprint: str | None = None


class QuestionItem(BaseModel):
    number: int
    status: QuestionStatus
    evidence: str = ""
    confidence: float = 1.0


class PlatformSnapshot(BaseModel):
    grade: str | float | None = None
    score: float | None = None
    comment_evidence: list[str] = Field(default_factory=list)
    graded_at: str | None = None


class AnswerSource(BaseModel):
    chapter: str = "习题一"
    resource_url: str | None = None
    pages_read: list[int] = Field(default_factory=list)
    imported_text: str | None = None
    imported_images: list[str] = Field(default_factory=list)
    source_type: str = "ta_import"  # ta_import|qr_url|none


class StudentResult(BaseModel):
    student_id: str
    student_name: str | None = None
    sortable_name: str | None = None
    submission: SubmissionMeta = Field(default_factory=SubmissionMeta)
    questions: list[QuestionItem] = Field(default_factory=list)
    proposed_score: int | float | None = None
    proposed_comment: str = ""
    final_score: int | float | None = None
    final_comment: str | None = None
    overall_confidence: float | None = None
    review_reasons: list[str] = Field(default_factory=list)
    platform_before: PlatformSnapshot = Field(default_factory=PlatformSnapshot)
    platform_after: PlatformSnapshot = Field(default_factory=PlatformSnapshot)
    authorization: str | None = None
    force_authorized: bool = False
    status: StudentStatus = "pending"
    writeback_step: WritebackStep = "idle"
    writeback_error: str | None = None
    verified_at: str | None = None
    approved_result_hash: str | None = None

    def effective_score(self) -> int | float | None:
        if self.final_score is not None:
            return self.final_score
        return self.proposed_score

    def effective_comment(self) -> str:
        if self.final_comment is not None:
            return self.final_comment
        return self.proposed_comment or ""


class GradingRun(BaseModel):
    id: str
    mode: Mode = "suggest"
    assignment_url: str
    course_id: int
    assignment_id: int
    assignment_name: str | None = None
    points_possible: float | None = 100
    rules_version: str
    question_numbers: list[int] = Field(default_factory=list)
    answer_source: AnswerSource = Field(default_factory=AnswerSource)
    students: list[StudentResult] = Field(default_factory=list)
    status: str = "created"  # created|fetched|grading|graded|writeback|done
    created_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")
    updated_at: str | None = None
    error: str | None = None


# --- API request/response shapes ---


class CredentialIn(BaseModel):
    token: str


class CredentialStatus(BaseModel):
    present: bool
    hint: str | None = None
    validated_at: str | None = None
    valid: bool = False
    user_name: str | None = None


class ModelCredentialIn(BaseModel):
    api_key: str


class ModelCredentialStatus(BaseModel):
    """Public model-key status. Never includes the key itself."""

    present: bool
    hint: str | None = None
    validated_at: str | None = None


class ResolveAssignmentIn(BaseModel):
    url: str


class ResolveAssignmentOut(BaseModel):
    course_id: int
    assignment_id: int
    name: str | None = None
    points_possible: float | None = None
    due_at: str | None = None
    submission_types: list[str] = Field(default_factory=list)
    question_numbers: list[int] = Field(default_factory=list)


class CreateRunIn(BaseModel):
    assignment_url: str
    mode: Mode = "suggest"
    question_numbers: list[int] | None = None
    answer_text: str | None = None
    download_attachments: bool = True


class AnswerImportIn(BaseModel):
    chapter: str = "习题一"
    resource_url: str | None = None
    text: str | None = None
    pages_read: list[int] = Field(default_factory=list)


class StudentPatchIn(BaseModel):
    final_score: int | float | None = None
    final_comment: str | None = None
    force_authorized: bool | None = None
    status: StudentStatus | None = None


class WritebackIn(BaseModel):
    student_ids: list[str]
    confirm: bool = False


class WritebackItemResult(BaseModel):
    student_id: str
    ok: bool
    status: str
    error: str | None = None
    steps_done: list[str] = Field(default_factory=list)
