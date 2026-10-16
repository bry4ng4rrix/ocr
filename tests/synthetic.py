"""Synthetic book pages with known ground truth, shared by every test module.

Pages are drawn with real TrueType fonts at a known size, so OCR, layout, style estimation and
PDF reconstruction can be checked against what was drawn. All functions return RGB uint8 arrays.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

FONT_CANDIDATES = {
    "serif": ["LiberationSerif-Regular.ttf", "DejaVuSerif.ttf", "NotoSerif-Regular.ttf"],
    "serif-bold": ["LiberationSerif-Bold.ttf", "DejaVuSerif-Bold.ttf", "NotoSerif-Bold.ttf"],
    "sans": ["LiberationSans-Regular.ttf", "DejaVuSans.ttf", "NotoSans-Regular.ttf"],
    "sans-bold": ["LiberationSans-Bold.ttf", "DejaVuSans-Bold.ttf", "NotoSans-Bold.ttf"],
}
FONT_DIRS = [Path(__file__).resolve().parent.parent / "assets" / "fonts", Path("/usr/share/fonts"),
             Path("/usr/local/share/fonts")]

PAGE_W, PAGE_H = 1240, 1754       # A5 at 300 dpi would be 1748 x 2480; this is A4 at 150 dpi
DPI = 150

FR_PARAGRAPH = ("Le château était silencieux à l'aube. Élise traversa la cour pavée, "
                "où l'on entendait à peine le bruissement des feuilles. Son cœur battait vite : "
                "aujourd'hui, elle découvrirait enfin la bibliothèque oubliée.")
EN_PARAGRAPH = ("The old library smelled of dust and paper. Rows of books stood in silence, "
                "waiting for a reader patient enough to open them again.")


@lru_cache(maxsize=None)
def font_path(style: str = "serif") -> str:
    for directory in FONT_DIRS:
        if not directory.exists():
            continue
        for name in FONT_CANDIDATES[style]:
            found = next(directory.rglob(name), None)
            if found:
                return str(found)
    raise RuntimeError(f"aucune police {style} trouvée pour les tests")


def font(size_px: int, style: str = "serif") -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(font_path(style), size_px)


@dataclass
class DrawnBlock:
    kind: str                  # "title", "paragraph", "caption", "page_number", "illustration"...
    text: str
    box: tuple[int, int, int, int]   # left, top, right, bottom in pixels
    size_px: int = 0
    color: tuple[int, int, int] = (0, 0, 0)

    def norm(self, w: int = PAGE_W, h: int = PAGE_H) -> tuple[float, float, float, float]:
        l, t, r, b = self.box
        return l / w, t / h, (r - l) / w, (b - t) / h


@dataclass
class SyntheticPage:
    image: np.ndarray
    blocks: list[DrawnBlock] = field(default_factory=list)

    def of_kind(self, kind: str) -> list[DrawnBlock]:
        return [b for b in self.blocks if b.kind == kind]


def _wrap(draw: ImageDraw.ImageDraw, text: str, fnt, width: int) -> list[str]:
    lines, current = [], ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if draw.textlength(candidate, font=fnt) <= width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def draw_paragraph(draw, text: str, left: int, top: int, width: int, size_px: int, style: str = "serif",
                   color=(0, 0, 0), leading: float = 1.35, align: str = "left") -> tuple[int, int, int, int]:
    """Draw wrapped text; returns the ink bounding box (left, top, right, bottom) in pixels."""
    fnt = font(size_px, style)
    lines = _wrap(draw, text, fnt, width)
    y = top
    ink = [10 ** 9, 10 ** 9, -1, -1]

    def grow(bbox):
        ink[0], ink[1] = min(ink[0], bbox[0]), min(ink[1], bbox[1])
        ink[2], ink[3] = max(ink[2], bbox[2]), max(ink[3], bbox[3])

    for i, line in enumerate(lines):
        x = left
        lw = draw.textlength(line, font=fnt)
        if align == "center":
            x = left + (width - lw) / 2
        elif align == "right":
            x = left + width - lw
        if align == "justify" and i < len(lines) - 1 and line.count(" "):
            words = line.split(" ")
            gap = (width - sum(draw.textlength(w, font=fnt) for w in words)) / (len(words) - 1)
            cx = left
            for w in words:
                draw.text((cx, y), w, font=fnt, fill=color)
                grow(draw.textbbox((cx, y), w, font=fnt))
                cx += draw.textlength(w, font=fnt) + gap
        else:
            draw.text((x, y), line, font=fnt, fill=color)
            grow(draw.textbbox((x, y), line, font=fnt))
        y += int(size_px * leading)
    return int(ink[0]), int(ink[1]), int(ink[2]), int(ink[3])


def illustration(width: int, height: int, dark: bool = False) -> np.ndarray:
    """A colourful picture: gradient sky, a sun, hills; `dark` makes it underexposed."""
    yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
    img = np.zeros((height, width, 3), np.float32)
    img[..., 0] = 60 + 120 * (yy / height)
    img[..., 1] = 120 + 80 * (xx / width)
    img[..., 2] = 230 - 100 * (yy / height)
    sun = (xx - width * 0.7) ** 2 + (yy - height * 0.3) ** 2 < (min(width, height) * 0.15) ** 2
    img[sun] = (250, 210, 40)
    hill = yy > height * (0.65 + 0.1 * np.sin(xx / width * 6.28))
    img[hill] = np.stack([40 + 0 * xx[hill], 140 + 30 * np.sin(xx[hill] / 17), 50 + 0 * xx[hill]], axis=-1)
    if dark:
        img *= 0.25
    return np.clip(img, 0, 255).astype(np.uint8)


def simple_page(paragraphs: list[str] | None = None, title: str = "Chapitre premier",
                size_px: int = 30, page_number: str = "12") -> SyntheticPage:
    image = Image.new("RGB", (PAGE_W, PAGE_H), "white")
    draw = ImageDraw.Draw(image)
    blocks = []
    top = 140
    if title:
        box = draw_paragraph(draw, title, 120, top, PAGE_W - 240, int(size_px * 2), "serif-bold", align="center")
        blocks.append(DrawnBlock("title", title, box, int(size_px * 2)))
        top = box[3] + 60
    for text in paragraphs or [FR_PARAGRAPH, EN_PARAGRAPH]:
        box = draw_paragraph(draw, text, 120, top, PAGE_W - 240, size_px)
        blocks.append(DrawnBlock("paragraph", text, box, size_px))
        top = box[3] + 50
    if page_number:
        box = draw_paragraph(draw, page_number, PAGE_W // 2 - 20, PAGE_H - 110, 60, int(size_px * 0.8))
        blocks.append(DrawnBlock("page_number", page_number, box, int(size_px * 0.8)))
    return SyntheticPage(np.array(image), blocks)


def two_column_page(size_px: int = 26) -> SyntheticPage:
    image = Image.new("RGB", (PAGE_W, PAGE_H), "white")
    draw = ImageDraw.Draw(image)
    blocks = []
    box = draw_paragraph(draw, "Deux colonnes", 100, 100, PAGE_W - 200, 56, "sans-bold", align="center")
    blocks.append(DrawnBlock("title", "Deux colonnes", box, 56))
    col_w = (PAGE_W - 200 - 60) // 2
    texts = [FR_PARAGRAPH, EN_PARAGRAPH, "Première colonne, second paragraphe : les éléments sont lus de haut en bas.",
             "Seconde colonne, dernier paragraphe : il vient après tous les autres dans l'ordre de lecture."]
    lefts = [100, 100, 100 + col_w + 60, 100 + col_w + 60]
    tops = [260, None, 260, None]
    for i, (text, left) in enumerate(zip(texts, lefts)):
        top = tops[i] if tops[i] is not None else blocks[-1].box[3] + 50
        box = draw_paragraph(draw, text, left, top, col_w, size_px)
        blocks.append(DrawnBlock("paragraph", text, box, size_px))
    return SyntheticPage(np.array(image), blocks)


def illustrated_page(size_px: int = 28, dark: bool = False) -> SyntheticPage:
    image = Image.new("RGB", (PAGE_W, PAGE_H), "white")
    draw = ImageDraw.Draw(image)
    blocks = []
    box = draw_paragraph(draw, "La vallée", 120, 110, PAGE_W - 240, 60, "serif-bold", align="center")
    blocks.append(DrawnBlock("title", "La vallée", box, 60))
    box = draw_paragraph(draw, FR_PARAGRAPH, 120, 230, PAGE_W - 240, size_px)
    blocks.append(DrawnBlock("paragraph", FR_PARAGRAPH, box, size_px))
    pic_box = (220, box[3] + 60, PAGE_W - 220, box[3] + 60 + 560)
    pic = illustration(pic_box[2] - pic_box[0], pic_box[3] - pic_box[1], dark=dark)
    image.paste(Image.fromarray(pic), pic_box[:2])
    blocks.append(DrawnBlock("illustration", "", pic_box))
    caption = "Figure 1. La vallée au lever du soleil."
    cbox = draw_paragraph(draw, caption, 220, pic_box[3] + 20, PAGE_W - 440, int(size_px * 0.8), align="center")
    blocks.append(DrawnBlock("caption", caption, cbox, int(size_px * 0.8)))
    box = draw_paragraph(draw, EN_PARAGRAPH, 120, cbox[3] + 60, PAGE_W - 240, size_px)
    blocks.append(DrawnBlock("paragraph", EN_PARAGRAPH, box, size_px))
    return SyntheticPage(np.array(image), blocks)


def rotate_page(page: np.ndarray, angle: float) -> np.ndarray:
    """Turn by `angle` degrees counter-clockwise on a white background (a skewed scan)."""
    h, w = page.shape[:2]
    matrix = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    return cv2.warpAffine(page, matrix, (w, h), flags=cv2.INTER_CUBIC, borderValue=(255, 255, 255))


def photographed(page: np.ndarray, background=(35, 30, 28), margin: float = 0.12, warp: float = 0.04,
                 shading: bool = True) -> np.ndarray:
    """The page as photographed on a dark stand: perspective, darker surroundings, uneven light."""
    h, w = page.shape[:2]
    out_w, out_h = int(w * (1 + 2 * margin)), int(h * (1 + 2 * margin))
    canvas = np.full((out_h, out_w, 3), background, np.uint8)
    mx, my = w * margin, h * margin
    dst = np.float32([[mx + w * warp, my], [mx + w - w * warp * 0.5, my + h * warp * 0.3],
                      [mx + w, my + h], [mx, my + h - h * warp * 0.2]])
    src = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
    matrix = cv2.getPerspectiveTransform(src, dst)
    warped = cv2.warpPerspective(page, matrix, (out_w, out_h), borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    mask = cv2.warpPerspective(np.full((h, w), 255, np.uint8), matrix, (out_w, out_h)) > 127
    canvas[mask] = warped[mask]
    if shading:
        xx = np.linspace(0.75, 1.0, out_w, dtype=np.float32)[None, :, None]
        canvas = np.clip(canvas.astype(np.float32) * xx, 0, 255).astype(np.uint8)
    return canvas


def color_cast(page: np.ndarray, gains=(1.0, 0.95, 0.75)) -> np.ndarray:
    """Wrong white balance (yellowish by default)."""
    return np.clip(page.astype(np.float32) * np.array(gains, np.float32), 0, 255).astype(np.uint8)


def save(page: np.ndarray, path: Path, dpi: int = DPI) -> Path:
    Image.fromarray(page).save(path, dpi=(dpi, dpi))
    return path
