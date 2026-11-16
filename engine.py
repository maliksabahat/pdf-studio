"""PDF Studio conversion engine.

Everything that touches PDFs, images, Office files and plain text lives here.
The Flask app in app.py only deals with uploads / downloads.
"""

from __future__ import annotations

import csv
import html
import io
import json
import os
import re
import zipfile
from html.parser import HTMLParser
from pathlib import Path

import pymupdf as fitz
from PIL import Image, ImageOps

# ---------------------------------------------------------------------------
# Optional HEIC support (pillow-heif is already present on this machine)
# ---------------------------------------------------------------------------
try:  # pragma: no cover - optional
    import pillow_heif

    pillow_heif.register_heif_opener()
except Exception:
    pass

Image.MAX_IMAGE_PIXELS = None

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
A4 = (595.28, 841.89)
LETTER = (612.0, 792.0)
LEGAL = (612.0, 1008.0)
PAGE_SIZES = {"a4": A4, "letter": LETTER, "legal": LEGAL}

IMAGE_EXTS = {
    ".png", ".jpg", ".jpeg", ".jfif", ".webp", ".bmp", ".gif", ".tif",
    ".tiff", ".heic", ".heif", ".avif", ".ico",
}
TEXT_EXTS = {
    ".txt", ".log", ".md", ".markdown", ".csv", ".tsv", ".json", ".xml",
    ".yaml", ".yml", ".ini", ".cfg", ".conf", ".env", ".rtf", ".html",
    ".htm", ".tex",
}
CODE_EXTS = {
    ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".css", ".scss",
    ".java", ".c", ".h", ".cpp", ".hpp", ".cs", ".php", ".rb", ".go", ".rs",
    ".swift", ".kt", ".sql", ".sh", ".bash", ".bat", ".ps1", ".vue", ".svelte",
}
OFFICE_EXTS = {".docx", ".docm", ".xlsx", ".xlsm", ".pptx", ".pptm"}
LEGACY_OFFICE = {".doc", ".xls", ".ppt"}
SUPPORTED_EXTS = {".pdf", *IMAGE_EXTS, *TEXT_EXTS, *CODE_EXTS, *OFFICE_EXTS}

BLACK = (0.12, 0.14, 0.19)
MUTED = (0.42, 0.45, 0.52)
ACCENT = (0.35, 0.24, 0.78)
RULE = (0.82, 0.84, 0.89)

_FONT_DIRS = [
    Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts",
    Path("/usr/share/fonts/truetype/dejavu"),
    Path("/usr/share/fonts/truetype/liberation"),
    Path("/System/Library/Fonts"),
]

_FONT_CANDIDATES = {
    "regular": ["segoeui.ttf", "arial.ttf", "calibri.ttf", "DejaVuSans.ttf", "LiberationSans-Regular.ttf"],
    "bold": ["segoeuib.ttf", "arialbd.ttf", "calibrib.ttf", "DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf"],
    "italic": ["segoeuii.ttf", "ariali.ttf", "calibrii.ttf", "DejaVuSans-Oblique.ttf", "LiberationSans-Italic.ttf"],
    "bolditalic": ["segoeuiz.ttf", "arialbi.ttf", "calibriz.ttf", "DejaVuSans-BoldOblique.ttf", "LiberationSans-BoldItalic.ttf"],
    "mono": ["consola.ttf", "cour.ttf", "DejaVuSansMono.ttf", "LiberationMono-Regular.ttf"],
}
_FONT_NAMES = {"regular": "F0", "bold": "F1", "italic": "F2", "bolditalic": "F3", "mono": "F4"}
_BUILTIN = {"regular": "helv", "bold": "hebo", "italic": "heit", "bolditalic": "hebi", "mono": "cour"}


class FontBook:
    def __init__(self) -> None:
        self.files: dict[str, str] = {}
        self.fonts: dict[str, fitz.Font] = {}
        for style, names in _FONT_CANDIDATES.items():
            for name in names:
                for folder in _FONT_DIRS:
                    candidate = folder / name
                    if candidate.exists():
                        self.files[style] = str(candidate)
                        try:
                            self.fonts[style] = fitz.Font(fontfile=str(candidate))
                        except Exception:
                            pass
                        break
                if style in self.files:
                    break
        # If a weight is missing, reuse the regular face so layout still works.
        for style in _FONT_NAMES:
            if style not in self.files and "regular" in self.files:
                self.files[style] = self.files["regular"]
                self.fonts[style] = self.fonts.get("regular")

    def measure(self, text: str, style: str, size: float) -> float:
        if not text:
            return 0.0
        font = self.fonts.get(style)
        try:
            if font is not None:
                return float(font.text_length(text, fontsize=size))
            return float(fitz.get_text_length(text, fontname=_BUILTIN[style], fontsize=size))
        except Exception:
            return len(text) * size * 0.52


BOOK = FontBook()


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def page_dims(size_key: str, landscape: bool = False) -> tuple[float, float]:
    w, h = PAGE_SIZES.get((size_key or "a4").lower(), A4)
    if landscape:
        w, h = h, w
    return w, h


def parse_page_range(spec: str, total: int) -> list[int]:
    """'1-3, 5, 8-' -> zero based page numbers. Empty / invalid -> all."""
    if not spec or not spec.strip():
        return list(range(total))
    pages: list[int] = []
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        m = re.fullmatch(r"(\d+)?\s*-\s*(\d+)?", chunk)
        if m:
            start = int(m.group(1)) if m.group(1) else 1
            end = int(m.group(2)) if m.group(2) else total
            for n in range(max(1, start), min(total, end) + 1):
                pages.append(n - 1)
        elif chunk.isdigit():
            n = int(chunk)
            if 1 <= n <= total:
                pages.append(n - 1)
    return sorted(set(pages)) or list(range(total))


def safe_filename(name: str, fallback: str = "document") -> str:
    name = (name or "").strip()
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return (name[:90] or fallback)


def _clean_text(text: str) -> str:
    """Normalise odd whitespace while keeping meaningful newlines."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\t", "    ")
    text = re.sub(r"[\u00ad\u200b\ufeff]", "", text)
    return text


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------
def normalize_image_bytes(data: bytes) -> tuple[bytes, int, int]:
    """EXIF-correct, alpha-flatten, RGB image bytes + pixel size."""
    with Image.open(io.BytesIO(data)) as im:
        source_format = (im.format or "").upper()
        im = ImageOps.exif_transpose(im)
        has_alpha = im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info)
        if has_alpha:
            im = im.convert("RGBA")
            background = Image.new("RGB", im.size, (255, 255, 255))
            background.paste(im, mask=im.split()[-1])
            im = background
        else:
            im = im.convert("RGB")
        if max(im.size) > 5000:
            im.thumbnail((5000, 5000), Image.LANCZOS)
        out = io.BytesIO()
        if source_format in ("JPEG", "MPO") and im.mode == "RGB":
            im.save(out, "JPEG", quality=92, optimize=True)
        else:
            im.save(out, "PNG", optimize=True)
        return out.getvalue(), im.width, im.height


def normalize_image_file(path: str | Path) -> tuple[bytes, int, int]:
    return normalize_image_bytes(Path(path).read_bytes())


def _crop_for_cover(data: bytes, target_ratio: float) -> bytes:
    with Image.open(io.BytesIO(data)) as im:
        im = im.convert("RGB")
        w, h = im.size
        ratio = w / h if h else target_ratio
        if ratio > target_ratio:
            new_w = max(1, int(h * target_ratio))
            left = (w - new_w) // 2
            im = im.crop((left, 0, left + new_w, h))
        else:
            new_h = max(1, int(w / target_ratio))
            top = (h - new_h) // 2
            im = im.crop((0, top, w, top + new_h))
        out = io.BytesIO()
        im.save(out, "JPEG", quality=93, optimize=True)
        return out.getvalue()


def image_page_size(data: bytes, iw: int, ih: int, options: dict) -> tuple[float, float, float]:
    """Return (page_width, page_height, margin) for an image page."""
    size_key = (options.get("pageSize") or "a4").lower()
    margin = float(options.get("margin", 0) or 0)
    if size_key == "auto":
        scale = 72.0 / 96.0
        w = min(max(iw * scale, 72), 1600)
        h = min(max(ih * scale, 72), 1600)
        return w + 2 * margin, h + 2 * margin, margin
    w, h = page_dims(size_key)
    if (options.get("orientation") or "auto") == "landscape":
        w, h = h, w
    return w, h, margin


def add_image_page(doc: fitz.Document, path: str | Path, options: dict) -> fitz.Page:
    data, iw, ih = normalize_image_file(path)
    w, h, margin = image_page_size(data, iw, ih, options)
    page = doc.new_page(width=w, height=h)
    avail = fitz.Rect(margin, margin, w - margin, h - margin)
    if avail.width <= 1 or avail.height <= 1:
        avail = fitz.Rect(0, 0, w, h)
    fit = (options.get("fit") or "contain").lower()
    if fit == "cover":
        data = _crop_for_cover(data, avail.width / max(avail.height, 1))
    page.insert_image(avail, stream=data, keep_proportion=True, overlay=True)
    return page


# ---------------------------------------------------------------------------
# Rich PDF writer (used for Word / Excel / PowerPoint / text)
# ---------------------------------------------------------------------------
class Writer:
    def __init__(
        self,
        width: float = A4[0],
        height: float = A4[1],
        margin: float = 54,
        base_size: float = 11,
    ) -> None:
        self.doc = fitz.open()
        self.width = width
        self.height = height
        self.margin = margin
        self.base_size = base_size
        self.page: fitz.Page | None = None
        self.y = margin
        self._new_page()

    # -- page management ---------------------------------------------------
    def _register_fonts(self, page: fitz.Page) -> None:
        for style, name in _FONT_NAMES.items():
            font_file = BOOK.files.get(style)
            if not font_file:
                continue
            try:
                page.insert_font(fontname=name, fontfile=font_file)
            except Exception:
                pass

    def _new_page(self) -> None:
        self.page = self.doc.new_page(width=self.width, height=self.height)
        self._register_fonts(self.page)
        self.y = self.margin

    def new_page(self) -> None:
        self._new_page()

    @property
    def bottom(self) -> float:
        return self.height - self.margin

    @property
    def content_width(self) -> float:
        return self.width - 2 * self.margin

    def _fontname(self, style: str) -> str:
        if style in BOOK.files:
            return _FONT_NAMES.get(style, "F0")
        return _BUILTIN.get(style, "helv")

    def _measure(self, text: str, style: str, size: float) -> float:
        return BOOK.measure(text, style, size)

    def _draw(self, page: fitz.Page, x: float, baseline: float, text: str,
              style: str, size: float, color: tuple[float, float, float]) -> None:
        if not text:
            return
        try:
            page.insert_text((x, baseline), text, fontname=self._fontname(style),
                             fontsize=size, color=color)
        except Exception:
            fallback = text.encode("latin-1", "replace").decode("latin-1")
            try:
                page.insert_text((x, baseline), fallback, fontname=_BUILTIN[style],
                                 fontsize=size, color=color)
            except Exception:
                pass

    # -- text wrapping -----------------------------------------------------
    def _wrap(self, text: str, max_width: float, style: str, size: float) -> list[str]:
        lines: list[str] = []
        for paragraph in _clean_text(text).split("\n"):
            words = paragraph.split(" ")
            line = ""
            for word in words:
                candidate = word if not line else line + " " + word
                if self._measure(candidate, style, size) <= max_width:
                    line = candidate
                    continue
                if line:
                    lines.append(line)
                if self._measure(word, style, size) > max_width:
                    pieces = self._split_long(word, max_width, style, size)
                    lines.extend(pieces[:-1])
                    line = pieces[-1] if pieces else ""
                else:
                    line = word
            lines.append(line)
        return lines or [""]

    def _split_long(self, word: str, max_width: float, style: str, size: float) -> list[str]:
        pieces: list[str] = []
        current = ""
        for ch in word:
            if current and self._measure(current + ch, style, size) > max_width:
                pieces.append(current)
                current = ch
            else:
                current += ch
        if current:
            pieces.append(current)
        return pieces or [word]

    def _x_for(self, line_width: float, align: str, left: float) -> float:
        if align == "center":
            return left + max(0.0, (self.content_width - line_width) / 2)
        if align == "right":
            return left + max(0.0, self.content_width - line_width)
        return left

    # -- public blocks -----------------------------------------------------
    def spacer(self, height: float) -> None:
        if height <= 0:
            return
        if self.y + height > self.bottom:
            self._new_page()
        else:
            self.y += height

    def paragraph(
        self,
        text: str,
        size: float | None = None,
        bold: bool = False,
        italic: bool = False,
        color: tuple[float, float, float] = BLACK,
        space_before: float = 0,
        space_after: float = 6,
        indent: float = 0,
        bullet: str | None = None,
        align: str = "left",
        line_height: float = 1.38,
        mono: bool = False,
    ) -> None:
        size = float(size or self.base_size)
        style = "mono" if mono else ("bolditalic" if bold and italic else "bold" if bold else "italic" if italic else "regular")
        max_width = max(10.0, self.content_width - indent)
        lines = self._wrap(text, max_width, style, size)
        lh = size * line_height

        self.spacer(space_before)
        for index, line in enumerate(lines):
            if self.y + lh > self.bottom + 0.5:
                self._new_page()
            left = self.margin + indent
            width = self._measure(line, style, size)
            x = self._x_for(width, align, left)
            baseline = self.y + size * 0.96
            if bullet and index == 0:
                bullet_style = "regular"
                bullet_x = max(self.margin, left - self._measure(bullet + " ", bullet_style, size))
                self._draw(self.page, bullet_x, baseline, bullet, bullet_style, size, color)
            self._draw(self.page, x, baseline, line, style, size, color)
            self.y += lh
        self.y += space_after

    def rich_paragraph(
        self,
        chunks: list[dict],
        size: float | None = None,
        color: tuple[float, float, float] = BLACK,
        space_before: float = 0,
        space_after: float = 6,
        indent: float = 0,
        bullet: str | None = None,
        align: str = "left",
        line_height: float = 1.38,
    ) -> None:
        size = float(size or self.base_size)
        max_width = max(10.0, self.content_width - indent)
        tokens: list[dict] = []
        for chunk in chunks:
            text = _clean_text(chunk.get("text", ""))
            style = chunk.get("style") or (
                "mono" if chunk.get("mono") else
                "bolditalic" if chunk.get("bold") and chunk.get("italic") else
                "bold" if chunk.get("bold") else
                "italic" if chunk.get("italic") else "regular"
            )
            chunk_size = float(chunk.get("size") or size)
            for part in re.split(r"(\n|\s+)", text):
                if not part:
                    continue
                if part == "\n":
                    tokens.append({"break": True})
                else:
                    tokens.append({
                        "text": part,
                        "space": part.isspace(),
                        "style": style,
                        "size": chunk_size,
                        "color": chunk.get("color") or color,
                    })

        lines: list[list[dict]] = [[]]
        width = 0.0
        for token in tokens:
            if token.get("break"):
                lines.append([])
                width = 0.0
                continue
            token_width = self._measure(token["text"], token["style"], token["size"])
            token["width"] = token_width
            if token["space"]:
                if width == 0:
                    continue
                lines[-1].append(token)
                width += token_width
                continue
            if width + token_width > max_width and width > 0:
                lines.append([])
                width = 0.0
            lines[-1].append(token)
            width += token_width

        self.spacer(space_before)
        for index, line in enumerate(lines):
            while line and line[-1].get("space"):
                line.pop()
            if not line:
                line_size = size
            else:
                line_size = max(t["size"] for t in line)
            lh = line_size * line_height
            if self.y + lh > self.bottom + 0.5:
                self._new_page()
            left = self.margin + indent
            line_width = sum(t.get("width", 0) for t in line)
            x = self._x_for(line_width, align, left)
            baseline = self.y + line_size * 0.96
            if bullet and index == 0:
                bullet_style = "regular"
                bx = max(self.margin, left - self._measure(bullet + " ", bullet_style, size))
                self._draw(self.page, bx, self.y + size * 0.96, bullet, bullet_style, size, color)
            for token in line:
                if not token["space"]:
                    self._draw(self.page, x, baseline, token["text"], token["style"],
                               token["size"], token["color"])
                x += token.get("width", 0)
            self.y += lh
        self.y += space_after

    def heading(self, text: str, level: int = 1, color: tuple[float, float, float] = (0.10, 0.12, 0.20)) -> None:
        sizes = {1: 21, 2: 17, 3: 14.5, 4: 12.5, 5: 11.5, 6: 11}
        before = {1: 16, 2: 14, 3: 11, 4: 9, 5: 8, 6: 7}
        after = {1: 9, 2: 7, 3: 5, 4: 4, 5: 3, 6: 3}
        level = max(1, min(6, int(level)))
        self.paragraph(text, size=sizes[level], bold=True, color=color,
                       space_before=before[level], space_after=after[level],
                       line_height=1.22)
        if level == 1:
            self.hr(color=(0.78, 0.80, 0.88), space=5)

    def hr(self, color: tuple[float, float, float] = RULE, space: float = 6) -> None:
        if self.y + 2 > self.bottom:
            self._new_page()
        self.page.draw_line((self.margin, self.y), (self.width - self.margin, self.y),
                            color=color, width=0.8)
        self.y += space

    def bullet_list(self, items: list[str], size: float | None = None, indent: float = 16) -> None:
        for item in items:
            self.paragraph(item, size=size, bullet="\u2022", indent=indent, space_after=3)

    def numbered_list(self, items: list[str], size: float | None = None, indent: float = 20) -> None:
        for index, item in enumerate(items, 1):
            self.paragraph(item, size=size, bullet=f"{index}.", indent=indent, space_after=3)

    def code_block(self, text: str, size: float = 8.8) -> None:
        lh = size * 1.35
        padding = 6
        wrapped_lines: list[str] = []
        for line in _clean_text(text).split("\n"):
            wrapped_lines.extend(self._wrap(line, self.content_width - 2 * padding, "mono", size) or [""])
        if not wrapped_lines:
            wrapped_lines = [""]

        self.spacer(3)
        index = 0
        while index < len(wrapped_lines):
            available = self.bottom - self.y - 2 * padding
            per_page = max(1, int(available // lh))
            chunk = wrapped_lines[index:index + per_page]
            index += len(chunk)
            block_height = len(chunk) * lh + 2 * padding
            self.page.draw_rect(
                fitz.Rect(self.margin, self.y, self.width - self.margin, self.y + block_height),
                color=None, fill=(0.955, 0.96, 0.975), width=0,
            )
            baseline = self.y + padding + size * 0.96
            for line in chunk:
                self._draw(self.page, self.margin + padding, baseline, line, "mono",
                           size, (0.15, 0.17, 0.23))
                baseline += lh
            self.y += block_height
            if index < len(wrapped_lines):
                self._new_page()
        self.y += 4

    def image(self, data: bytes, caption: str | None = None,
              max_width: float | None = None, max_height: float | None = None) -> None:
        data, iw, ih = normalize_image_bytes(data)
        if iw <= 0 or ih <= 0:
            return
        max_width = max_width or self.content_width
        max_height = max_height or (self.bottom - self.margin) * 0.82
        scale = min(max_width / iw, max_height / ih, 1.0)
        draw_w, draw_h = iw * scale, ih * scale
        if self.y + draw_h > self.bottom and draw_h <= (self.bottom - self.margin):
            self._new_page()
        x = self.margin + max(0.0, (self.content_width - draw_w) / 2)
        rect = fitz.Rect(x, self.y, x + draw_w, self.y + draw_h)
        try:
            self.page.insert_image(rect, stream=data, keep_proportion=True, overlay=True)
        except Exception:
            return
        self.y += draw_h + 4
        if caption:
            self.paragraph(caption, size=8.5, italic=True, color=MUTED, align="center", space_after=8)

    def table(self, rows: list[list[str]], header: bool = True, font_size: float = 9.0,
              max_cols: int = 14) -> None:
        rows = [[_cell_text(cell) for cell in row] for row in rows]
        rows = [row for row in rows if any(cell.strip() for cell in row)]
        if not rows:
            return
        truncated_cols = max(len(row) for row in rows) > max_cols
        rows = [row[:max_cols] for row in rows]
        n_cols = max(len(row) for row in rows)
        if n_cols == 0:
            return
        for row in rows:
            row.extend([""] * (n_cols - len(row)))

        # Column widths based on content length (with sensible caps).
        weights = []
        for col in range(n_cols):
            longest = max(len(row[col]) for row in rows)
            weights.append(min(max(longest, 4), 44))
        total_weight = sum(weights) or 1
        widths = [self.content_width * w / total_weight for w in weights]

        padding = 4.0
        lh = font_size * 1.28
        max_chars = 900
        prepared = [[cell[:max_chars] for cell in row] for row in rows]

        def cell_lines(cell: str, width: float) -> list[str]:
            return self._wrap(cell, max(8.0, width - 2 * padding), "regular", font_size)

        def row_height(cells: list[str]) -> float:
            line_count = max(len(cell_lines(cell, widths[i])) for i, cell in enumerate(cells))
            return max(line_count * lh + 2 * padding, lh + 2 * padding)

        header_row = prepared[0] if header else None
        body = prepared[1:] if header else prepared
        if header and row_height(header_row) > (self.bottom - self.margin):
            header = False
            header_row = None
            body = prepared

        self.spacer(4)
        self._draw_table_header(header_row, widths, padding, lh, font_size, n_cols)

        for row in body:
            h = row_height(row)
            if self.y + h > self.bottom + 0.5:
                self._new_page()
                if header_row is not None:
                    self._draw_table_header(header_row, widths, padding, lh, font_size, n_cols)
            self._draw_table_row(row, widths, padding, lh, font_size)
        self.y += 4
        if truncated_cols:
            self.paragraph(f"Only the first {max_cols} columns are shown.",
                           size=8, italic=True, color=MUTED, space_before=2, space_after=6)

    def _draw_table_header(self, row: list[str] | None, widths: list[float], padding: float,
                           lh: float, font_size: float, n_cols: int) -> None:
        if row is None:
            return
        h = max(max(len(self._wrap(cell, max(8.0, widths[i] - 2 * padding), "regular", font_size))
                    for i, cell in enumerate(row)) * lh + 2 * padding, lh + 2 * padding)
        if self.y + h > self.bottom:
            self._new_page()
        x = self.margin
        self.page.draw_rect(fitz.Rect(x, self.y, x + sum(widths), self.y + h),
                            color=None, fill=(0.925, 0.93, 0.955), width=0)
        for i, cell in enumerate(row):
            lines = self._wrap(cell, max(8.0, widths[i] - 2 * padding), "regular", font_size)
            baseline = self.y + padding + font_size * 0.96
            for line in lines:
                self._draw(self.page, x + padding, baseline, line, "bold", font_size, BLACK)
                baseline += lh
            self.page.draw_rect(fitz.Rect(x, self.y, x + widths[i], self.y + h),
                                color=(0.76, 0.78, 0.85), width=0.5)
            x += widths[i]
        self.y += h

    def _draw_table_row(self, row: list[str], widths: list[float], padding: float,
                        lh: float, font_size: float) -> None:
        lines_per_cell = [self._wrap(cell, max(8.0, widths[i] - 2 * padding), "regular", font_size)
                          for i, cell in enumerate(row)]
        line_count = max(len(lines) for lines in lines_per_cell) if lines_per_cell else 1
        h = max(line_count * lh + 2 * padding, lh + 2 * padding)
        x = self.margin
        for i, lines in enumerate(lines_per_cell):
            baseline = self.y + padding + font_size * 0.96
            for line in lines:
                self._draw(self.page, x + padding, baseline, line, "regular", font_size, BLACK)
                baseline += lh
            self.page.draw_rect(fitz.Rect(x, self.y, x + widths[i], self.y + h),
                                color=(0.82, 0.84, 0.89), width=0.4)
            x += widths[i]
        self.y += h

    def text_in_rect(self, rect: fitz.Rect, text: str, size: float = 16,
                     bold: bool = False, color: tuple[float, float, float] = BLACK,
                     align: int = 0) -> None:
        style = "bold" if bold else "regular"
        fontname = self._fontname(style)
        current = max(6.0, size)
        while current >= 6:
            result = self.page.insert_textbox(
                rect, text, fontname=fontname, fontsize=current, color=color,
                align=align, lineheight=1.18,
            )
            if result >= 0:
                return
            current -= 1

    def finish(self) -> fitz.Document:
        return self.doc


# ---------------------------------------------------------------------------
# DOCX
# ---------------------------------------------------------------------------
def _cell_text(value) -> str:
    if value is None:
        return ""
    if hasattr(value, "text"):
        return str(value.text)
    return str(value)


def _iter_docx_blocks(doc):
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    for child in doc.element.body.iterchildren():
        tag = child.tag
        if tag.endswith("}p"):
            yield Paragraph(child, doc)
        elif tag.endswith("}tbl"):
            yield Table(child, doc)


def _docx_style_size(style, default: float) -> float:
    try:
        if style is not None and style.font is not None and style.font.size is not None:
            return float(style.font.size.pt)
        if style is not None and style.base_style is not None:
            return _docx_style_size(style.base_style, default)
    except Exception:
        pass
    return default


def _docx_style_bold(style) -> bool:
    try:
        if style is not None and style.font is not None and style.font.bold is not None:
            return bool(style.font.bold)
        if style is not None and style.base_style is not None:
            return _docx_style_bold(style.base_style)
    except Exception:
        pass
    return False


def _docx_chunks(paragraph, default_size: float, default_bold: bool = False) -> list[dict]:
    chunks: list[dict] = []
    style_bold = default_bold or _docx_style_bold(paragraph.style)
    for run in paragraph.runs:
        text = run.text
        if not text:
            continue
        try:
            size = float(run.font.size.pt) if run.font.size is not None else default_size
        except Exception:
            size = default_size
        font_name = ""
        try:
            font_name = (run.font.name or "").lower()
        except Exception:
            pass
        is_mono = any(key in font_name for key in ("consolas", "courier", "mono", "code"))
        chunks.append({
            "text": text,
            "bold": bool(run.bold) if run.bold is not None else style_bold,
            "italic": bool(run.italic) if run.italic is not None else False,
            "size": size,
            "mono": is_mono,
        })
    if not chunks and paragraph.text:
        chunks.append({"text": paragraph.text, "bold": style_bold, "size": default_size})
    return chunks


def _docx_images(paragraph, doc) -> list[bytes]:
    from docx.oxml.ns import qn

    images: list[bytes] = []
    seen: set[str] = set()
    try:
        blips = paragraph._p.xpath(".//a:blip")
    except Exception:
        blips = []
    for blip in blips:
        rid = blip.get(qn("r:embed"))
        if not rid or rid in seen:
            continue
        seen.add(rid)
        try:
            part = doc.part.related_parts[rid]
            blob = part.blob
            if blob:
                images.append(blob)
        except Exception:
            continue
    return images


def docx_to_doc(path: str | Path, options: dict) -> fitz.Document:
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    source = Document(str(path))
    landscape = (options.get("orientation") or "auto") == "landscape"
    width, height = page_dims(options.get("pageSize", "a4"), landscape)
    margin = float(options.get("margin", 54))
    writer = Writer(width, height, margin=margin, base_size=11)

    normal = source.styles["Normal"] if "Normal" in source.styles else None
    base_size = _docx_style_size(normal, 11)

    align_map = {
        WD_ALIGN_PARAGRAPH.CENTER: "center",
        WD_ALIGN_PARAGRAPH.RIGHT: "right",
    }
    number_counter = 0
    for block in _iter_docx_blocks(source):
        if block.__class__.__name__ == "Paragraph":
            text = block.text.strip()
            style_name = (block.style.name or "").lower() if block.style else ""
            images = _docx_images(block, source)
            align = align_map.get(block.alignment, "left")
            size = _docx_style_size(block.style, base_size)

            if not text and not images:
                writer.spacer(4)
                continue

            if style_name.startswith("heading"):
                match = re.search(r"(\d)", style_name)
                level = int(match.group(1)) if match else 1
                writer.heading(text, level)
                number_counter = 0
            elif "title" in style_name and "subtitle" not in style_name:
                writer.paragraph(text, size=max(size + 8, 24), bold=True, align=align,
                                 space_before=4, space_after=10, line_height=1.2)
                number_counter = 0
            elif "subtitle" in style_name:
                writer.paragraph(text, size=max(size + 2, 13), italic=True, color=MUTED,
                                 align=align, space_after=10)
            elif "list bullet" in style_name:
                writer.paragraph(text, size=size, bullet="\u2022", indent=18, space_after=3)
            elif "list number" in style_name:
                number_counter += 1
                writer.paragraph(text, size=size, bullet=f"{number_counter}.", indent=20, space_after=3)
            elif "quote" in style_name:
                writer.paragraph(text, size=size, italic=True, color=MUTED, indent=16, space_after=6)
            else:
                number_counter = 0
                chunks = _docx_chunks(block, size)
                writer.rich_paragraph(chunks, size=size, align=align, space_after=6)
            for blob in images:
                writer.image(blob)
        else:  # Table
            rows = []
            for row in block.rows:
                rows.append([cell.text for cell in row.cells])
            writer.table(rows, header=True)
            writer.spacer(6)
            number_counter = 0

    return writer.finish()


# ---------------------------------------------------------------------------
# XLSX
# ---------------------------------------------------------------------------
def _format_cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return f"{value:,.4f}".rstrip("0").rstrip(".")
    if hasattr(value, "strftime"):
        try:
            if getattr(value, "hour", 0) or getattr(value, "minute", 0):
                return value.strftime("%Y-%m-%d %H:%M")
            return value.strftime("%Y-%m-%d")
        except Exception:
            return str(value)
    return str(value)


_FORMULA_FN = re.compile(r"^=\s*(SUM|AVERAGE|AVG|MIN|MAX|COUNT)\s*\(([^()]*)\)\s*$", re.I)
_CELL_REF = re.compile(r"^\$?([A-Za-z]{1,3})\$?(\d+)$")
_RANGE_REF = re.compile(r"^\$?([A-Za-z]{1,3})\$?(\d+)\s*:\s*\$?([A-Za-z]{1,3})\$?(\d+)$")


def _eval_formula(formula: str, grid: dict) -> int | float | None:
    """Evaluate the handful of spreadsheet functions people actually use.

    Returns None when the formula is outside this small subset, in which case
    the caller shows the formula text instead of a blank cell.
    """
    from openpyxl.utils import column_index_from_string

    match = _FORMULA_FN.match(formula)
    if not match:
        return None
    fn = match.group(1).upper()
    numbers: list[float] = []

    for argument in match.group(2).split(","):
        argument = argument.strip()
        if not argument:
            continue
        cell_range = _RANGE_REF.match(argument)
        if cell_range:
            col1 = column_index_from_string(cell_range.group(1).upper())
            row1 = int(cell_range.group(2))
            col2 = column_index_from_string(cell_range.group(3).upper())
            row2 = int(cell_range.group(4))
            for row in range(min(row1, row2), max(row1, row2) + 1):
                for col in range(min(col1, col2), max(col1, col2) + 1):
                    value = grid.get((row, col))
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        numbers.append(float(value))
            continue
        cell = _CELL_REF.match(argument)
        if cell:
            col = column_index_from_string(cell.group(1).upper())
            row = int(cell.group(2))
            value = grid.get((row, col))
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                numbers.append(float(value))
            continue
        try:
            numbers.append(float(argument.replace("%", "")))
        except ValueError:
            return None

    if fn == "COUNT":
        return len(numbers)
    if not numbers:
        return 0 if fn == "SUM" else None
    if fn == "SUM":
        result = sum(numbers)
    elif fn in ("AVERAGE", "AVG"):
        result = sum(numbers) / len(numbers)
    elif fn == "MIN":
        result = min(numbers)
    elif fn == "MAX":
        result = max(numbers)
    else:
        return None
    return int(result) if float(result).is_integer() else round(result, 6)


def xlsx_to_doc(path: str | Path, options: dict) -> fitz.Document:
    from openpyxl import load_workbook

    workbook = load_workbook(str(path), data_only=False, read_only=True)
    max_cols = 14
    max_rows = 5000

    # Decide orientation from the widest sheet.
    widest = 1
    for sheet in workbook.worksheets:
        try:
            widest = max(widest, min(sheet.max_column or 1, max_cols))
        except Exception:
            pass
    orientation = (options.get("orientation") or "auto").lower()
    landscape = orientation == "landscape" or (orientation == "auto" and widest > 6)
    width, height = page_dims(options.get("pageSize", "a4"), landscape)
    margin = float(options.get("margin", 48))
    writer = Writer(width, height, margin=margin, base_size=10)

    for sheet_index, sheet in enumerate(workbook.worksheets):
        if sheet_index:
            writer.new_page()
        title = sheet.title or f"Sheet {sheet_index + 1}"
        writer.heading(title, 2)

        raw_rows: list[tuple] = []
        grid: dict[tuple[int, int], object] = {}
        truncated_rows = False
        for row_index, row in enumerate(sheet.iter_rows(values_only=True), start=1):
            if row_index > max_rows:
                truncated_rows = True
                break
            raw_rows.append(row)
            for col_index, value in enumerate(row[:max_cols], start=1):
                grid[(row_index, col_index)] = value

        rows: list[list[str]] = []
        for row_index, row in enumerate(raw_rows, start=1):
            values = []
            for col_index, value in enumerate(row[:max_cols], start=1):
                if isinstance(value, str) and value.startswith("="):
                    computed = _eval_formula(value, grid)
                    if computed is not None:
                        value = computed
                values.append(_format_cell(value))
            while values and not values[-1].strip():
                values.pop()
            if any(value.strip() for value in values):
                rows.append(values)

        if rows:
            writer.table(rows, header=True, font_size=9, max_cols=max_cols)
        else:
            writer.paragraph("(empty sheet)", size=9, italic=True, color=MUTED)
        if truncated_rows:
            writer.paragraph(f"Only the first {max_rows} rows are shown.",
                             size=8, italic=True, color=MUTED)

    try:
        workbook.close()
    except Exception:
        pass
    return writer.finish()


# ---------------------------------------------------------------------------
# PPTX
# ---------------------------------------------------------------------------
def _shape_rect(shape) -> fitz.Rect:
    return fitz.Rect(
        shape.left / 12700.0,
        shape.top / 12700.0,
        (shape.left + shape.width) / 12700.0,
        (shape.top + shape.height) / 12700.0,
    )


def pptx_to_doc(path: str | Path, options: dict) -> fitz.Document:
    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE_TYPE, PP_PLACEHOLDER

    presentation = Presentation(str(path))
    width = presentation.slide_width / 12700.0
    height = presentation.slide_height / 12700.0
    writer = Writer(width, height, margin=0, base_size=18)

    for slide_index, slide in enumerate(presentation.slides):
        if slide_index:
            writer.new_page()
        page = writer.page
        for shape in slide.shapes:
            try:
                rect = _shape_rect(shape)
            except Exception:
                continue
            if rect.width <= 1 or rect.height <= 1:
                continue

            if getattr(shape, "has_table", False) and shape.has_table:
                rows = [[cell.text for cell in row.cells] for row in shape.table.rows]
                text = "\n".join(" | ".join(row) for row in rows)
                writer.text_in_rect(rect, text, size=10, color=BLACK)
                continue

            if getattr(shape, "has_text_frame", False) and shape.has_text_frame:
                paragraphs = [p.text for p in shape.text_frame.paragraphs if p.text.strip()]
                if not paragraphs:
                    continue
                text = "\n".join(paragraphs)
                is_title = False
                try:
                    is_title = shape.is_placeholder and shape.placeholder_format.type in (
                        PP_PLACEHOLDER.TITLE, PP_PLACEHOLDER.CENTER_TITLE)
                except Exception:
                    is_title = False
                size = 34 if is_title else 18
                bold = is_title
                for paragraph in shape.text_frame.paragraphs:
                    for run in paragraph.runs:
                        try:
                            if run.font.size is not None:
                                size = float(run.font.size.pt)
                            if run.font.bold:
                                bold = True
                        except Exception:
                            pass
                    if size:
                        break
                writer.text_in_rect(rect, text, size=size, bold=bold)
                continue

            if getattr(shape, "shape_type", None) == MSO_SHAPE_TYPE.PICTURE or hasattr(shape, "image"):
                try:
                    data = shape.image.blob
                except Exception:
                    continue
                data, iw, ih = normalize_image_bytes(data)
                if iw <= 0 or ih <= 0:
                    continue
                scale = min(rect.width / iw, rect.height / ih)
                draw_w, draw_h = iw * scale, ih * scale
                x = rect.x0 + (rect.width - draw_w) / 2
                y = rect.y0 + (rect.height - draw_h) / 2
                try:
                    page.insert_image(fitz.Rect(x, y, x + draw_w, y + draw_h),
                                      stream=data, keep_proportion=True, overlay=True)
                except Exception:
                    pass
        _ = page

    return writer.finish()


# ---------------------------------------------------------------------------
# Plain text / Markdown / CSV / HTML / RTF
# ---------------------------------------------------------------------------
def read_text_file(path: str | Path) -> str:
    data = Path(path).read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


_INLINE_MD = re.compile(r"(\*\*[^*]+\*\*|__[^_]+__|\*[^*]+\*|_[^_]+_|`[^`]+`|\[[^\]]+\]\([^)]+\))")


def _inline_md_chunks(text: str, size: float) -> list[dict]:
    chunks: list[dict] = []
    for part in _INLINE_MD.split(text):
        if not part:
            continue
        if part.startswith("**") and part.endswith("**"):
            chunks.append({"text": part[2:-2], "bold": True, "size": size})
        elif part.startswith("__") and part.endswith("__"):
            chunks.append({"text": part[2:-2], "bold": True, "size": size})
        elif part.startswith("`") and part.endswith("`"):
            chunks.append({"text": part[1:-1], "mono": True, "size": size * 0.92})
        elif (part.startswith("*") and part.endswith("*")) or (part.startswith("_") and part.endswith("_")):
            chunks.append({"text": part[1:-1], "italic": True, "size": size})
        elif part.startswith("[") and "](" in part:
            label = part[1:part.index("]")]
            chunks.append({"text": label, "size": size})
        else:
            chunks.append({"text": part, "size": size})
    return chunks or [{"text": text, "size": size}]


def _markdown_to_writer(writer: Writer, text: str) -> None:
    lines = _clean_text(text).split("\n")
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()

        if stripped.startswith("```"):
            index += 1
            code: list[str] = []
            while index < len(lines) and not lines[index].strip().startswith("```"):
                code.append(lines[index])
                index += 1
            writer.code_block("\n".join(code))
            index += 1
            continue

        if not stripped:
            writer.spacer(5)
            index += 1
            continue

        if re.fullmatch(r"(-{3,}|\*{3,}|_{3,})", stripped):
            writer.hr(space=8)
            index += 1
            continue

        heading = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if heading:
            writer.heading(heading.group(2), len(heading.group(1)))
            index += 1
            continue

        if stripped.startswith(">"):
            quote = stripped.lstrip(">").strip()
            writer.paragraph(quote, italic=True, color=MUTED, indent=16, space_after=5)
            index += 1
            continue

        if re.match(r"^[-*+]\s+", stripped):
            items = []
            while index < len(lines) and re.match(r"^\s*[-*+]\s+", lines[index]):
                items.append(re.sub(r"^\s*[-*+]\s+", "", lines[index]).strip())
                index += 1
            for item in items:
                writer.rich_paragraph(_inline_md_chunks(item, writer.base_size),
                                      bullet="\u2022", indent=18, space_after=2)
            continue

        if re.match(r"^\d+[.)]\s+", stripped):
            items = []
            while index < len(lines) and re.match(r"^\s*\d+[.)]\s+", lines[index]):
                items.append(re.sub(r"^\s*\d+[.)]\s+", "", lines[index]).strip())
                index += 1
            for number, item in enumerate(items, 1):
                writer.rich_paragraph(_inline_md_chunks(item, writer.base_size),
                                      bullet=f"{number}.", indent=20, space_after=2)
            continue

        # Pipe table
        if "|" in stripped and index + 1 < len(lines) and re.match(r"^\s*\|?[\s:|-]+\|[\s:|-]*$", lines[index + 1]):
            rows = []
            while index < len(lines) and "|" in lines[index]:
                raw = lines[index].strip().strip("|")
                rows.append([cell.strip() for cell in raw.split("|")])
                index += 1
            if len(rows) >= 2:
                rows.pop(1)  # separator row
            writer.table(rows, header=True, font_size=9)
            writer.spacer(4)
            continue

        # Paragraph (join consecutive plain lines)
        paragraph = [stripped]
        index += 1
        while index < len(lines):
            nxt = lines[index].strip()
            if (not nxt or nxt.startswith("#") or nxt.startswith("```") or nxt.startswith(">")
                    or re.match(r"^[-*+]\s+", nxt) or re.match(r"^\d+[.)]\s+", nxt)
                    or re.fullmatch(r"(-{3,}|\*{3,}|_{3,})", nxt)):
                break
            paragraph.append(nxt)
            index += 1
        writer.rich_paragraph(_inline_md_chunks(" ".join(paragraph), writer.base_size), space_after=7)


def text_to_doc(path: str | Path, options: dict, markdown: bool = False) -> fitz.Document:
    landscape = (options.get("orientation") or "auto") == "landscape"
    width, height = page_dims(options.get("pageSize", "a4"), landscape)
    margin = float(options.get("margin", 54))
    writer = Writer(width, height, margin=margin, base_size=11)
    text = read_text_file(path)
    if markdown:
        _markdown_to_writer(writer, text)
    else:
        text = _clean_text(text)
        for line in text.split("\n"):
            if not line.strip():
                writer.spacer(5)
            else:
                writer.paragraph(line, space_after=1, line_height=1.32)
    return writer.finish()


def csv_to_doc(path: str | Path, options: dict) -> fitz.Document:
    landscape = (options.get("orientation") or "auto") == "landscape"
    width, height = page_dims(options.get("pageSize", "a4"), landscape)
    margin = float(options.get("margin", 48))
    writer = Writer(width, height, margin=margin, base_size=10)
    raw = read_text_file(path)
    delimiter = "\t" if str(path).lower().endswith(".tsv") else ","
    rows = []
    for row in csv.reader(io.StringIO(raw), delimiter=delimiter):
        rows.append([cell.strip() for cell in row])
        if len(rows) >= 5000:
            break
    if rows:
        writer.table(rows, header=True, font_size=9)
    else:
        writer.paragraph("(empty file)", italic=True, color=MUTED)
    return writer.finish()


class _HTMLBlocks(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[tuple] = []
        self.buffer: list[str] = []
        self.kind: tuple | None = None
        self.skip = 0
        self.table_rows: list[list[str]] = []
        self.row: list[str] | None = None
        self.cell: list[str] | None = None

    def flush(self) -> None:
        text = re.sub(r"\s+", " ", "".join(self.buffer)).strip()
        self.buffer = []
        if text and self.kind:
            self.blocks.append((self.kind, text))
        self.kind = None

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag in ("script", "style", "noscript"):
            self.skip += 1
            return
        if self.skip:
            return
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.flush()
            self.kind = ("heading", int(tag[1]))
        elif tag in ("p", "div", "section", "article", "header", "footer"):
            self.flush()
            self.kind = ("paragraph", 0)
        elif tag == "li":
            self.flush()
            self.kind = ("bullet", 0)
        elif tag == "blockquote":
            self.flush()
            self.kind = ("quote", 0)
        elif tag == "br":
            self.buffer.append("\n")
        elif tag == "table":
            self.flush()
            self.table_rows = []
        elif tag == "tr":
            self.row = []
        elif tag in ("td", "th"):
            self.cell = []
        elif tag == "img":
            alt = dict(attrs).get("alt")
            if alt:
                self.buffer.append(alt)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in ("script", "style", "noscript"):
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6", "p", "div", "section",
                   "article", "header", "footer", "li", "blockquote"):
            self.flush()
        elif tag in ("td", "th"):
            if self.row is not None and self.cell is not None:
                self.row.append(re.sub(r"\s+", " ", "".join(self.cell)).strip())
            self.cell = None
        elif tag == "tr":
            if self.row is not None and self.table_rows is not None:
                self.table_rows.append(self.row)
            self.row = None
        elif tag == "table":
            if self.table_rows:
                self.blocks.append((("table", 0), self.table_rows))
            self.table_rows = []

    def handle_data(self, data: str) -> None:
        if self.skip:
            return
        if self.cell is not None:
            self.cell.append(data)
        else:
            self.buffer.append(data)


def html_to_doc(path: str | Path, options: dict) -> fitz.Document:
    landscape = (options.get("orientation") or "auto") == "landscape"
    width, height = page_dims(options.get("pageSize", "a4"), landscape)
    margin = float(options.get("margin", 54))
    writer = Writer(width, height, margin=margin, base_size=11)
    parser = _HTMLBlocks()
    parser.feed(read_text_file(path))
    parser.close()
    for kind, content in parser.blocks:
        if kind[0] == "heading":
            writer.heading(content, kind[1])
        elif kind[0] == "bullet":
            writer.paragraph(content, bullet="\u2022", indent=18, space_after=3)
        elif kind[0] == "quote":
            writer.paragraph(content, italic=True, color=MUTED, indent=16, space_after=6)
        elif kind[0] == "table":
            writer.table(content, header=True, font_size=9)
            writer.spacer(5)
        else:
            writer.paragraph(content, space_after=6)
    return writer.finish()


def _strip_rtf_groups(text: str) -> str:
    """Remove font tables, colour tables and other metadata groups."""
    for name in ("fonttbl", "colortbl", "stylesheet", "info", "pict", "object", "*"):
        opener = re.compile(r"\{\\" + re.escape(name) + r"\b")
        while True:
            match = opener.search(text)
            if not match:
                break
            depth = 0
            index = match.start()
            while index < len(text):
                char = text[index]
                if char == "{":
                    depth += 1
                elif char == "}":
                    depth -= 1
                    if depth == 0:
                        index += 1
                        break
                index += 1
            text = text[:match.start()] + text[index:]
    return text


def rtf_to_text(raw: str) -> str:
    text = _strip_rtf_groups(raw)
    text = re.sub(r"\\'([0-9a-fA-F]{2})", lambda m: bytes([int(m.group(1), 16)]).decode("cp1252", "replace"), text)
    text = re.sub(r"\\u(-?\d+)\??", lambda m: chr(int(m.group(1)) % 65536), text)
    text = text.replace("\\par", "\n").replace("\\line", "\n").replace("\\tab", "    ")
    text = re.sub(r"\\[a-zA-Z]+-?\d* ?", "", text)
    text = text.replace("{", "").replace("}", "")
    text = text.replace("\\", "")
    return _clean_text(text)


def rtf_to_doc(path: str | Path, options: dict) -> fitz.Document:
    landscape = (options.get("orientation") or "auto") == "landscape"
    width, height = page_dims(options.get("pageSize", "a4"), landscape)
    margin = float(options.get("margin", 54))
    writer = Writer(width, height, margin=margin, base_size=11)
    text = rtf_to_text(read_text_file(path))
    for line in text.split("\n"):
        if line.strip():
            writer.paragraph(line, space_after=2)
        else:
            writer.spacer(5)
    return writer.finish()


# ---------------------------------------------------------------------------
# Mixed file dispatch
# ---------------------------------------------------------------------------
def file_kind(path: str | Path) -> str:
    ext = Path(path).suffix.lower()
    if ext == ".pdf":
        return "pdf"
    if ext in IMAGE_EXTS:
        return "image"
    if ext in OFFICE_EXTS:
        return "office"
    if ext in LEGACY_OFFICE:
        return "legacy"
    if ext in TEXT_EXTS or ext in CODE_EXTS:
        return "text"
    return "unknown"


def mixed_file_to_doc(path: str | Path, options: dict) -> fitz.Document:
    path = Path(path)
    ext = path.suffix.lower()

    if ext == ".pdf":
        return fitz.open(str(path))
    if ext in IMAGE_EXTS:
        doc = fitz.open()
        image_options = dict(options)
        image_options.setdefault("pageSize", options.get("pageSize", "a4"))
        add_image_page(doc, path, image_options)
        return doc
    if ext in (".docx", ".docm"):
        return docx_to_doc(path, options)
    if ext in (".xlsx", ".xlsm"):
        return xlsx_to_doc(path, options)
    if ext in (".pptx", ".pptm"):
        return pptx_to_doc(path, options)
    if ext in (".csv", ".tsv"):
        return csv_to_doc(path, options)
    if ext in (".md", ".markdown"):
        return text_to_doc(path, options, markdown=True)
    if ext in (".html", ".htm"):
        return html_to_doc(path, options)
    if ext == ".rtf":
        return rtf_to_doc(path, options)
    if ext in (".json", ".xml", ".yaml", ".yml"):
        text = read_text_file(path)
        if ext == ".json":
            try:
                text = json.dumps(json.loads(text), indent=2, ensure_ascii=False)
            except Exception:
                pass
        landscape = (options.get("orientation") or "auto") == "landscape"
        width, height = page_dims(options.get("pageSize", "a4"), landscape)
        writer = Writer(width, height, margin=float(options.get("margin", 48)), base_size=9)
        writer.code_block(text, size=8.5)
        return writer.finish()
    if ext in CODE_EXTS:
        landscape = (options.get("orientation") or "auto") == "landscape"
        width, height = page_dims(options.get("pageSize", "a4"), landscape)
        writer = Writer(width, height, margin=float(options.get("margin", 48)), base_size=9)
        writer.code_block(read_text_file(path), size=8.5)
        return writer.finish()
    if ext in TEXT_EXTS:
        return text_to_doc(path, options)

    # Unknown extension: if it decodes as text, render it; otherwise reject.
    try:
        data = path.read_bytes()
        data.decode("utf-8")
        return text_to_doc(path, options)
    except Exception as exc:
        raise ValueError(f"Unsupported file type: {path.suffix or 'unknown'}") from exc


# ---------------------------------------------------------------------------
# Page studio build
# ---------------------------------------------------------------------------
def build_pages_pdf(items: list[dict], options: dict, lookup) -> fitz.Document:
    if not items:
        raise ValueError("No pages to build.")
    output = fitz.open()
    uniform = bool(options.get("uniform"))
    size_key = options.get("pageSize", "a4")
    fit_mode = (options.get("fit") or "contain").lower()
    margin = float(options.get("margin", 0) or 0)

    for item in items:
        meta = lookup(item["uploadId"])
        if meta is None:
            raise ValueError("An uploaded file is missing. Please add it again.")
        source_path = meta["path"]
        rotation = int(item.get("rotation", 0)) % 360
        rotation = (rotation // 90) * 90

        if meta["kind"] == "pdf":
            src = fitz.open(source_path)
            try:
                page_index = int(item.get("page", 0))
                if page_index < 0 or page_index >= len(src):
                    continue
                source_page = src[page_index]
                # show_pdf_page() ignores a page's own /Rotate flag. Bake it into
                # the content first so the page looks the way a viewer shows it.
                if source_page.rotation:
                    try:
                        source_page.remove_rotation()
                    except Exception:
                        pass
                src_w, src_h = source_page.rect.width, source_page.rect.height
                if uniform:
                    w, h = page_dims(size_key)
                    # Keep landscape pages landscape instead of squeezing them
                    # into a portrait sheet.
                    landscape = src_w > src_h
                    if rotation in (90, 270):
                        landscape = not landscape
                    if landscape:
                        w, h = h, w
                    page = output.new_page(width=w, height=h)
                    content = fitz.Rect(margin, margin, w - margin, h - margin)
                    if content.width <= 1 or content.height <= 1:
                        content = page.rect
                    page.show_pdf_page(content, src, page_index, keep_proportion=True, rotate=rotation)
                else:
                    w, h = src_w, src_h
                    if rotation in (90, 270):
                        w, h = h, w
                    page = output.new_page(width=w, height=h)
                    page.show_pdf_page(page.rect, src, page_index, rotate=rotation)
            finally:
                src.close()
        else:
            image_options = {
                "pageSize": "auto" if options.get("imagePageSize") == "auto" else size_key,
                "fit": fit_mode,
                "margin": margin,
                "orientation": "auto",
            }
            if options.get("imagePageSize") == "auto":
                image_options["pageSize"] = "auto"
            add_image_page(output, source_path, image_options)
            if rotation:
                page = output[-1]
                page.set_rotation(rotation)

    if len(output) == 0:
        raise ValueError("The result is empty. Add at least one page.")
    return output


# ---------------------------------------------------------------------------
# PDF -> images
# ---------------------------------------------------------------------------
def pdf_to_images_zip(path: str | Path, fmt: str = "png", dpi: int = 150,
                      quality: int = 90, page_spec: str = "",
                      stem_name: str | None = None) -> tuple[bytes, int, str]:
    fmt = (fmt or "png").lower()
    fmt = {"jpeg": "jpg", "tif": "png", "tiff": "png"}.get(fmt, fmt)
    if fmt not in ("png", "jpg", "webp"):
        fmt = "png"
    dpi = max(36, min(int(dpi or 150), 900))
    quality = max(20, min(int(quality or 90), 100))

    doc = fitz.open(str(path))
    try:
        pages = parse_page_range(page_spec, len(doc))
        zoom = dpi / 72.0
        stem = safe_filename(stem_name or Path(path).stem, "pages")
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for page_index in pages:
                page = doc[page_index]
                pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
                if fmt == "png":
                    data = pix.tobytes("png")
                elif fmt == "jpg":
                    data = pix.tobytes("jpg", jpg_quality=quality)
                else:
                    image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                    out = io.BytesIO()
                    image.save(out, "WEBP", quality=quality, method=4)
                    data = out.getvalue()
                archive.writestr(f"{stem}_page_{page_index + 1:03d}.{fmt}", data)
        return buffer.getvalue(), len(pages), f"{stem}_{fmt}.zip"
    finally:
        doc.close()


def pdf_page_image(path: str | Path, page_index: int, fmt: str = "png",
                   dpi: int = 150, quality: int = 90,
                   stem_name: str | None = None) -> tuple[bytes, str]:
    """Render one PDF page as an image and return its bytes and filename."""
    fmt = (fmt or "png").lower()
    fmt = {"jpeg": "jpg", "tif": "png", "tiff": "png"}.get(fmt, fmt)
    if fmt not in ("png", "jpg", "webp"):
        fmt = "png"
    dpi = max(36, min(int(dpi or 150), 900))
    quality = max(20, min(int(quality or 90), 100))

    doc = fitz.open(str(path))
    try:
        page_index = int(page_index)
        if page_index < 0 or page_index >= len(doc):
            raise ValueError("Page number is out of range.")
        page = doc[page_index]
        pix = page.get_pixmap(matrix=fitz.Matrix(dpi / 72.0, dpi / 72.0), alpha=False)
        if fmt == "png":
            data = pix.tobytes("png")
        elif fmt == "jpg":
            data = pix.tobytes("jpg", jpg_quality=quality)
        else:
            image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            out = io.BytesIO()
            image.save(out, "WEBP", quality=quality, method=4)
            data = out.getvalue()
        stem = safe_filename(stem_name or Path(path).stem, "page")
        return data, f"{stem}_page_{page_index + 1:03d}.{fmt}"
    finally:
        doc.close()


def render_thumbnail(path: str | Path, page_index: int, rotation: int = 0,
                     target_width: int = 260) -> bytes:
    rotation = (int(rotation or 0) // 90 * 90) % 360
    if Path(path).suffix.lower() in IMAGE_EXTS:
        data, iw, ih = normalize_image_file(path)
        with Image.open(io.BytesIO(data)) as image:
            image = image.convert("RGB")
            if rotation:
                image = image.rotate(-rotation, expand=True)
            image.thumbnail((target_width, target_width * 2), Image.LANCZOS)
            out = io.BytesIO()
            image.save(out, "JPEG", quality=82, optimize=True)
            return out.getvalue()
    doc = fitz.open(str(path))
    try:
        page_index = max(0, min(int(page_index), len(doc) - 1))
        page = doc[page_index]
        zoom = target_width / max(page.rect.width, 1)
        zoom = max(0.05, min(zoom, 4.0))
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        data = pix.tobytes("jpg", jpg_quality=82)
        if rotation:
            with Image.open(io.BytesIO(data)) as image:
                image = image.rotate(-rotation, expand=True)
                out = io.BytesIO()
                image.save(out, "JPEG", quality=82, optimize=True)
                return out.getvalue()
        return data
    finally:
        doc.close()


def image_thumbnail(path: str | Path, target_width: int = 260) -> bytes:
    return render_thumbnail(path, 0, 0, target_width)
