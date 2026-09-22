"""Question bank path + official answer import (no publisher login required)."""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any

from app.config import Settings, get_settings
from app.models import AnswerSource

logger = logging.getLogger(__name__)

# Known metadata from docs/02 (for recording, not for live login)
EXERCISE_1_ANSWER_META = {
    "chapter": "习题一",
    "qr_url": "http://2d.hep.cn/1251876/12",
    "resource_url": (
        "https://abooks.hep.com.cn/h5/resource/detail"
        "?resourceId=F3W7LQZYU&projectId=P3ECHAZ9R"
    ),
    "qr_page": 5,
}


class QuestionBank:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.pdf_path = self.settings.resolve_path(self.settings.question_bank_pdf)
        self.work_dir = self.settings.repo_root() / "work" / "question-bank"
        self.answers_dir = self.settings.repo_root() / "local" / "answers"
        self.answers_dir.mkdir(parents=True, exist_ok=True)

    def pdf_exists(self) -> bool:
        return self.pdf_path.is_file()

    def status(self) -> dict[str, Any]:
        return {
            "pdf_configured": str(self.pdf_path),
            "pdf_exists": self.pdf_exists(),
            "exercise_1_meta": EXERCISE_1_ANSWER_META,
            "imported_answer_files": [p.name for p in self.answers_dir.glob("*")],
        }

    def import_answer_text(
        self,
        text: str,
        *,
        chapter: str = "习题一",
        resource_url: str | None = None,
        pages_read: list[int] | None = None,
    ) -> AnswerSource:
        """TA paste/upload of official answers — preferred V1 path."""
        dest = self.answers_dir / f"{chapter.replace(' ', '_')}.txt"
        dest.write_text(text, encoding="utf-8")
        return AnswerSource(
            chapter=chapter,
            resource_url=resource_url or EXERCISE_1_ANSWER_META["resource_url"],
            pages_read=pages_read or [],
            imported_text=text,
            source_type="ta_import",
        )

    def load_imported_text(self, chapter: str = "习题一") -> str | None:
        dest = self.answers_dir / f"{chapter.replace(' ', '_')}.txt"
        if dest.is_file():
            return dest.read_text(encoding="utf-8")
        return None

    def render_pages(self, pages: list[int] | None = None, dpi: int = 150) -> list[Path]:
        """Render PDF pages to PNG under work/ (requires pdftoppm)."""
        if not self.pdf_exists():
            raise FileNotFoundError(f"Question bank PDF not found: {self.pdf_path}")
        self.work_dir.mkdir(parents=True, exist_ok=True)
        out: list[Path] = []
        if pages:
            for p in pages:
                prefix = self.work_dir / f"page-{p}"
                subprocess.run(
                    [
                        "pdftoppm",
                        "-f",
                        str(p),
                        "-l",
                        str(p),
                        "-r",
                        str(dpi),
                        "-singlefile",
                        "-png",
                        str(self.pdf_path),
                        str(prefix),
                    ],
                    check=True,
                )
                png = prefix.with_suffix(".png")
                if png.is_file():
                    out.append(png)
        else:
            prefix = self.work_dir / "page"
            subprocess.run(
                [
                    "pdftoppm",
                    "-r",
                    str(dpi),
                    "-png",
                    str(self.pdf_path),
                    str(prefix),
                ],
                check=True,
            )
            out = sorted(self.work_dir.glob("page-*.png"))
        return out

    def try_decode_qr_page(self, page: int = 5) -> dict[str, Any]:
        """Optional: run tools/decode_qr.py for chapter answer meta."""
        script = self.settings.repo_root() / "tools" / "decode_qr.py"
        if not script.is_file() or not self.pdf_exists():
            return {"ok": False, "reason": "missing_pdf_or_script"}
        try:
            proc = subprocess.run(
                ["python3", str(script), str(self.pdf_path), "--page", str(page)],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if proc.returncode != 0:
                return {
                    "ok": False,
                    "reason": "decode_failed",
                    "stderr": (proc.stderr or "")[:200],
                }
            import json

            return {"ok": True, "data": json.loads(proc.stdout)}
        except Exception as exc:  # noqa: BLE001
            logger.warning("QR decode skipped: %s", type(exc).__name__)
            return {"ok": False, "reason": type(exc).__name__}


_qb: QuestionBank | None = None


def get_question_bank() -> QuestionBank:
    global _qb
    if _qb is None:
        _qb = QuestionBank()
    return _qb
