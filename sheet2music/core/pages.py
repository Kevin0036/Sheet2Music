"""PDF → 页面图（pdftoppm）：首页预览 + 编号页面导出。

编号规则与 `run_homr_trial.py` 一致：只认 `page-数字.png` 这种命名，
实验衍生图（如 `page-1-cut.png`）一律忽略。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import cv2
import numpy as np

from .settings import pdftoppm_binary

NUMBERED_PAGE_PATTERN = re.compile(r"^page-(\d+)\.png$")


def numbered_page_paths(pages_dir: Path) -> list[Path]:
    numbered: list[tuple[int, Path]] = []
    for path in pages_dir.glob("page-*.png"):
        match = NUMBERED_PAGE_PATTERN.match(path.name)
        if match is None:
            continue
        numbered.append((int(match.group(1)), path))
    return [path for _, path in sorted(numbered)]


def _group_consecutive_rows(rows: np.ndarray) -> list[np.ndarray]:
    if rows.size == 0:
        return []
    groups: list[list[int]] = [[int(rows[0])]]
    for row in rows[1:]:
        value = int(row)
        if value == groups[-1][-1] + 1:
            groups[-1].append(value)
        else:
            groups.append([value])
    return [np.asarray(group) for group in groups]


def _staff_line_groups(image: np.ndarray) -> list[np.ndarray]:
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    dark = cv2.threshold(gray, 200, 1, cv2.THRESH_BINARY_INV)[1]
    kernel_width = max(25, int(round(image.shape[1] * 0.15)))
    horizontal = cv2.morphologyEx(
        dark,
        cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_RECT, (kernel_width, 1)),
    )
    row_hits = horizontal.sum(axis=1)
    min_line_length = max(100, int(round(image.shape[1] * 0.08)))
    rows = np.flatnonzero(row_hits >= min_line_length)
    return _group_consecutive_rows(rows)


def _staff_unit_size(groups: list[np.ndarray]) -> float | None:
    if len(groups) < 5:
        return None
    first_staff = np.asarray([float(group.mean()) for group in groups[:5]])
    gaps = np.diff(first_staff)
    unit = float(np.median(gaps))
    if unit <= 0 or float(np.max(gaps)) > unit * 1.8 or float(np.min(gaps)) < unit * 0.5:
        return None
    return unit


def detect_music_vertical_bounds(
    image: np.ndarray,
    top_margin_spaces: float = 8.0,
    bottom_margin_spaces: float = 10.0,
) -> tuple[int, int]:
    """Find a safe vertical crop around the first and last detected staff systems.

    The crop is deliberately based on long horizontal staff lines rather than the
    page's generic ink bounding box, so title and copyright text do not become part
    of HOMR's input. If a page has an unusual layout, returning the full page is
    safer than making an aggressive crop.
    """
    height = image.shape[0]
    groups = _staff_line_groups(image)
    unit = _staff_unit_size(groups)
    if unit is None:
        return 0, height

    first_line = float(groups[0].mean())
    last_line = float(groups[-1].mean())
    top = max(0, int(round(first_line - top_margin_spaces * unit)))
    bottom = min(height, int(round(last_line + bottom_margin_spaces * unit)))
    if bottom <= top or bottom - top < height * 0.2:
        return 0, height
    return top, bottom


def crop_page_vertically(
    source_path: Path,
    target_path: Path,
    top_margin_spaces: float = 8.0,
    bottom_margin_spaces: float = 10.0,
    geometry_path: Path | None = None,
) -> tuple[int, int]:
    """Write a vertically cropped page and return its ``(top, bottom)`` bounds."""
    image = cv2.imread(str(source_path), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError(f"无法读取页面图像: {source_path}")
    top, bottom = detect_music_vertical_bounds(
        image,
        top_margin_spaces=top_margin_spaces,
        bottom_margin_spaces=bottom_margin_spaces,
    )
    target_path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(target_path), image[top:bottom, :])
    if geometry_path is not None:
        write_page_geometry(
            geometry_path,
            raw_width=image.shape[1],
            raw_height=image.shape[0],
            top=top,
            bottom=bottom,
        )
    return top, bottom


def write_page_geometry(
    geometry_path: Path,
    raw_width: int,
    raw_height: int,
    top: int,
    bottom: int,
) -> None:
    payload = {
        "schema_version": 1,
        "raw_size": {"width": raw_width, "height": raw_height},
        "input_bounds_in_raw": [0, top, raw_width, bottom],
        "input_size": {"width": raw_width, "height": bottom - top},
    }
    geometry_path.parent.mkdir(parents=True, exist_ok=True)
    geometry_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def ensure_page_geometry(raw_page: Path, page: Path, geometry_path: Path) -> bool:
    if geometry_path.exists():
        return True
    raw_image = cv2.imread(str(raw_page), cv2.IMREAD_COLOR)
    input_image = cv2.imread(str(page), cv2.IMREAD_COLOR)
    if raw_image is None or input_image is None:
        return False

    top, bottom = detect_music_vertical_bounds(raw_image)
    if input_image.shape[:2] != (bottom - top, raw_image.shape[1]):
        if input_image.shape[:2] != raw_image.shape[:2]:
            return False
        top, bottom = 0, raw_image.shape[0]
    write_page_geometry(
        geometry_path,
        raw_width=raw_image.shape[1],
        raw_height=raw_image.shape[0],
        top=top,
        bottom=bottom,
    )
    return True


def export_numbered_pages(
    pdf_path: Path,
    pages_dir: Path,
    dpi: int = 600,
    crop_vertical: bool = True,
) -> list[Path]:
    """Render high-resolution pages, then crop vertical non-score margins."""
    pages_dir.mkdir(parents=True, exist_ok=True)
    existing = numbered_page_paths(pages_dir)
    if existing:
        raw_pages_dir = pages_dir / "raw"
        geometry_dir = pages_dir / "geometry"
        for page in existing:
            ensure_page_geometry(
                raw_pages_dir / page.name,
                page,
                geometry_dir / f"{page.stem}.json",
            )
        return existing
    raw_pages_dir = pages_dir / "raw"
    raw_pages_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            pdftoppm_binary(),
            "-png",
            "-r", str(dpi),
            str(pdf_path),
            str(raw_pages_dir / "page"),
        ],
        check=True,
    )
    raw_pages = numbered_page_paths(raw_pages_dir)
    if not raw_pages:
        raise FileNotFoundError(f"No page images were exported from {pdf_path}")
    pages: list[Path] = []
    geometry_dir = pages_dir / "geometry"
    for raw_page in raw_pages:
        page = pages_dir / raw_page.name
        geometry_path = geometry_dir / f"{raw_page.stem}.json"
        if crop_vertical:
            crop_page_vertically(raw_page, page, geometry_path=geometry_path)
        else:
            shutil.copy2(raw_page, page)
            image = cv2.imread(str(raw_page), cv2.IMREAD_COLOR)
            if image is None:
                raise ValueError(f"无法读取页面图像: {raw_page}")
            write_page_geometry(
                geometry_path,
                raw_width=image.shape[1],
                raw_height=image.shape[0],
                top=0,
                bottom=image.shape[0],
            )
        pages.append(page)
    return pages


def extract_first_page_preview(pdf_path: Path, preview_dir: Path, dpi: int = 150) -> Path:
    """只渲染第一页，用于浏览器预览。

    pdftoppm 默认会按总页数补零（如 10 页 PDF 输出 page-01.png），而预览接口
    按 page-1.png 查找，因此加 `-singlefile` 强制输出固定文件名 page-1.png。
    """
    preview_dir.mkdir(parents=True, exist_ok=True)
    preview_path = preview_dir / "page-1.png"
    if preview_path.exists():
        return preview_path
    subprocess.run(
        [
            pdftoppm_binary(),
            "-png",
            "-f", "1",
            "-l", "1",
            "-singlefile",
            "-r", str(dpi),
            str(pdf_path),
            str(preview_dir / "page-1"),
        ],
        check=True,
    )
    if not preview_path.exists():
        raise FileNotFoundError(f"Preview extraction failed for {pdf_path}")
    return preview_path
