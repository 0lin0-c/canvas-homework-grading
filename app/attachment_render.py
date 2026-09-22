"""Turn submission attachments into page images for the vision model."""
from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


def _page_sort_key(path: Path) -> int:
    match = re.search(r"-(\d+)$", path.stem)
    return int(match.group(1)) if match else 0


def _pdf_page_count(path: Path) -> int | None:
    try:
        proc = subprocess.run(
            ["pdfinfo", str(path)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    for line in (proc.stdout or "").splitlines():
        if line.lower().startswith("pages:"):
            try:
                return int(line.split(":", 1)[1].strip())
            except ValueError:
                return None
    return None


def _render_pdf(pdf: Path, prefix: Path, *, dpi: int, max_pages: int) -> tuple[list[Path], bool]:
    """Render up to max_pages PNG pages. Second value is True when every page was rendered."""
    if max_pages <= 0:
        return [], False
    total = _pdf_page_count(pdf)
    render_n = max_pages if total is None else min(total, max_pages)
    if render_n <= 0:
        return [], False
    prefix.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "pdftoppm",
        "-png",
        "-r",
        str(dpi),
        "-f",
        "1",
        "-l",
        str(render_n),
        str(pdf),
        str(prefix),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=90)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        logger.warning("pdftoppm failed for attachment render")
        return [], False
    pages = sorted(prefix.parent.glob(prefix.name + "-*.png"), key=_page_sort_key)
    pages = pages[:max_pages]
    if not pages:
        return [], False
    if total is None:
        # Unknown length: a full cap may have dropped trailing pages.
        fully = len(pages) < max_pages
    else:
        fully = total <= max_pages and len(pages) >= min(total, max_pages)
    return pages, fully


def attachments_to_page_images(
    paths: list[str],
    *,
    max_pages: int,
    work_dir: Path,
    dpi: int = 150,
) -> tuple[list[Path], bool]:
    """Return (image_paths, fully_read).

    Images pass through. PDFs are rendered with pdftoppm in page order.
    fully_read is False when a file is missing, unsupported, failed to render,
    or the page cap dropped pages.
    """
    if max_pages <= 0:
        return [], False
    work_dir.mkdir(parents=True, exist_ok=True)
    images: list[Path] = []
    fully = True
    for idx, raw in enumerate(paths):
        path = Path(raw)
        if not path.is_file():
            fully = False
            continue
        suffix = path.suffix.lower()
        if suffix in IMAGE_SUFFIXES:
            if len(images) >= max_pages:
                fully = False
                continue
            images.append(path)
            continue
        if suffix == ".pdf":
            remaining = max_pages - len(images)
            if remaining <= 0:
                fully = False
                continue
            rendered, pdf_full = _render_pdf(
                path,
                work_dir / f"att{idx}",
                dpi=dpi,
                max_pages=remaining,
            )
            images.extend(rendered)
            if not pdf_full:
                fully = False
            continue
        fully = False
    return images, fully
