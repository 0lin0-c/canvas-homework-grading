"""Canvas REST API client for oc.sjtu.edu.cn (server-side token only)."""
from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from app.config import Settings, get_settings
from app.vault import TokenVault, get_vault

logger = logging.getLogger(__name__)

_ASSIGNMENT_PATH = re.compile(
    r"/courses/(?P<course_id>\d+)/assignments/(?P<assignment_id>\d+)"
)
_SPEEDGRADER = re.compile(r"/courses/(?P<course_id>\d+)/gradebook/speed_grader")


class CanvasAuthError(Exception):
    pass


class CanvasAPIError(Exception):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def resolve_assignment_url(url: str, allowed_host: str = "oc.sjtu.edu.cn") -> dict[str, int]:
    """Parse Canvas assignment or SpeedGrader URL; reject wrong host."""
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower()
    if host != allowed_host.lower():
        raise ValueError(f"host must be {allowed_host}, got {host or '(none)'}")

    m = _ASSIGNMENT_PATH.search(parsed.path or "")
    if m:
        return {
            "course_id": int(m.group("course_id")),
            "assignment_id": int(m.group("assignment_id")),
        }

    m = _SPEEDGRADER.search(parsed.path or "")
    if m:
        qs = parse_qs(parsed.query or "")
        aid = qs.get("assignment_id", [None])[0]
        if not aid:
            raise ValueError("SpeedGrader URL missing assignment_id")
        return {
            "course_id": int(m.group("course_id")),
            "assignment_id": int(aid),
        }

    raise ValueError("URL does not look like a Canvas assignment link")


def submission_fingerprint(submitted_at: str | None, attempt: int | None, attachment_ids: list[str]) -> str:
    raw = f"{submitted_at or ''}|{attempt or ''}|{','.join(sorted(attachment_ids))}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


class CanvasClient:
    def __init__(
        self,
        settings: Settings | None = None,
        vault: TokenVault | None = None,
        timeout: float = 60.0,
    ) -> None:
        self.settings = settings or get_settings()
        self.vault = vault or get_vault()
        self.timeout = timeout
        self.base = self.settings.canvas_base_url.rstrip("/")

    def _token(self) -> str:
        token = self.vault.get_token()
        if not token:
            raise CanvasAuthError("No Canvas token in vault")
        return token

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token()}",
            "Accept": "application/json",
        }

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.base,
            headers=self._headers(),
            timeout=self.timeout,
            follow_redirects=True,
        )

    def _raise_for_status(self, resp: httpx.Response) -> None:
        if resp.status_code in (401, 403):
            raise CanvasAuthError(f"Canvas auth failed HTTP {resp.status_code}")
        if resp.status_code >= 400:
            # Never include response body if it might echo token; keep short.
            raise CanvasAPIError(
                f"Canvas API error HTTP {resp.status_code}",
                status_code=resp.status_code,
            )

    def get_self(self) -> dict[str, Any]:
        with self._client() as c:
            r = c.get("/users/self")
            self._raise_for_status(r)
            return r.json()

    def get_assignment(self, course_id: int, assignment_id: int) -> dict[str, Any]:
        with self._client() as c:
            r = c.get(f"/courses/{course_id}/assignments/{assignment_id}")
            self._raise_for_status(r)
            return r.json()

    def list_submissions(
        self,
        course_id: int,
        assignment_id: int,
        *,
        per_page: int = 100,
    ) -> list[dict[str, Any]]:
        """Paginate submissions with user, history, comments."""
        results: list[dict[str, Any]] = []
        params: list[tuple[str, str]] = [
            ("per_page", str(per_page)),
            ("include[]", "user"),
            ("include[]", "submission_history"),
            ("include[]", "submission_comments"),
        ]
        url: str | None = f"/courses/{course_id}/assignments/{assignment_id}/submissions"
        with self._client() as c:
            while url:
                r = c.get(url, params=params if url.startswith("/") else None)
                self._raise_for_status(r)
                chunk = r.json()
                if isinstance(chunk, list):
                    results.extend(chunk)
                else:
                    raise CanvasAPIError("Unexpected submissions payload")
                # Link header pagination
                next_url = _parse_link_next(r.headers.get("Link") or r.headers.get("link"))
                if next_url and next_url.startswith(self.base):
                    url = next_url[len(self.base) :]
                    params = []  # already in next URL
                elif next_url and next_url.startswith("http"):
                    # Absolute URL on same host — use full request
                    r2 = c.get(next_url)
                    self._raise_for_status(r2)
                    chunk2 = r2.json()
                    if isinstance(chunk2, list):
                        results.extend(chunk2)
                    next_url = _parse_link_next(r2.headers.get("Link") or r2.headers.get("link"))
                    while next_url:
                        r2 = c.get(next_url)
                        self._raise_for_status(r2)
                        chunk2 = r2.json()
                        if isinstance(chunk2, list):
                            results.extend(chunk2)
                        next_url = _parse_link_next(r2.headers.get("Link") or r2.headers.get("link"))
                    break
                else:
                    break
        logger.info(
            "Listed %d submissions for course=%s assignment=%s",
            len(results),
            course_id,
            assignment_id,
        )
        return results

    def download_attachment(self, url: str, dest: Path) -> Path:
        """Download attachment via authorized request; strip query from logs."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        safe_log = url.split("?")[0]
        logger.info("Downloading attachment %s -> %s", safe_log, dest.name)
        with self._client() as c:
            # Attachment URLs may be absolute on object storage
            if url.startswith("http"):
                r = c.get(url)
            else:
                r = c.get(url)
            self._raise_for_status(r)
            dest.write_bytes(r.content)
        return dest

    def get_submission(
        self,
        course_id: int,
        assignment_id: int,
        user_id: str | int,
        *,
        include_comments: bool = True,
    ) -> dict[str, Any]:
        params: list[tuple[str, str]] = []
        if include_comments:
            params.append(("include[]", "submission_comments"))
            params.append(("include[]", "user"))
        with self._client() as c:
            r = c.get(
                f"/courses/{course_id}/assignments/{assignment_id}/submissions/{user_id}",
                params=params,
            )
            self._raise_for_status(r)
            return r.json()

    def put_grade(
        self,
        course_id: int,
        assignment_id: int,
        user_id: str | int,
        grade: str | float | int,
    ) -> dict[str, Any]:
        data = {"submission[posted_grade]": str(grade)}
        with self._client() as c:
            r = c.put(
                f"/courses/{course_id}/assignments/{assignment_id}/submissions/{user_id}",
                data=data,
            )
            self._raise_for_status(r)
            return r.json()

    def put_comment(
        self,
        course_id: int,
        assignment_id: int,
        user_id: str | int,
        text: str,
    ) -> dict[str, Any]:
        data = {"comment[text_comment]": text}
        with self._client() as c:
            r = c.put(
                f"/courses/{course_id}/assignments/{assignment_id}/submissions/{user_id}",
                data=data,
            )
            self._raise_for_status(r)
            return r.json()


def _parse_link_next(link_header: str) -> str | None:
    if not link_header:
        return None
    # <url>; rel="next"
    for part in link_header.split(","):
        if 'rel="next"' in part or "rel=next" in part:
            m = re.search(r"<([^>]+)>", part)
            if m:
                return m.group(1)
    return None


def normalize_submission(raw: dict[str, Any]) -> dict[str, Any]:
    """Map Canvas submission JSON to our fields."""
    user = raw.get("user") or {}
    user_id = str(raw.get("user_id") or user.get("id") or "")
    attachments_raw = raw.get("attachments") or []
    attachments = []
    for a in attachments_raw:
        attachments.append(
            {
                "id": str(a.get("id")),
                "name": a.get("filename") or a.get("display_name") or "",
                "url": a.get("url"),
                "size": a.get("size"),
            }
        )
    comments = []
    for c in raw.get("submission_comments") or []:
        body = (c.get("comment") or "").strip()
        if body:
            comments.append(body)
    submitted_at = raw.get("submitted_at")
    attempt = raw.get("attempt")
    fp = submission_fingerprint(
        submitted_at,
        attempt,
        [a["id"] for a in attachments],
    )
    workflow = raw.get("workflow_state")
    has_submit = bool(
        submitted_at
        or workflow in ("submitted", "graded", "pending_review")
        and attachments
    )
    # Untyped / unsubmitted
    if workflow == "unsubmitted" and not attachments:
        has_submit = False

    return {
        "student_id": user_id,
        "student_name": user.get("name"),
        "sortable_name": user.get("sortable_name"),
        "submission": {
            "submitted_at": submitted_at,
            "attempt": attempt,
            "workflow_state": workflow,
            "attachments": attachments,
            "fingerprint": fp,
        },
        "platform_before": {
            "grade": raw.get("grade"),
            "score": raw.get("score"),
            "comment_evidence": comments,
            "graded_at": raw.get("graded_at"),
        },
        "has_submission": has_submit or bool(attachments) or bool(submitted_at),
    }
