"""Pages de test synthétiques avec vérité terrain (texte, boîtes, images, catégorie, transformations).

Rendu avec de vraies polices TrueType (Liberation, sinon DejaVu) à 200 dpi sur un format A5.
Toutes les boîtes de vérité sont en pixels (gauche, haut, droite, bas) de `SyntheticPage.image`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from models import BlockKind, Box, ImageKind, OcrResult, OcrWord, PageCategory, PageFormat, STANDARD_FORMATS

DPI = 200
PX_PER_PT = DPI / 72.0
A5 = STANDARD_FORMATS["A5"]
PAGE_W = int(round(A5.width_mm / 25.4 * DPI))     # 1165
PAGE_H = int(round(A5.height_mm / 25.4 * DPI))    # 1654
MARGIN = int(0.12 * PAGE_W)
PAPER = (250, 248, 242)
INK = (25, 25, 25)

FONT_CANDIDATES = {
    ("serif", False, False): ["liberation/LiberationSerif-Regular.ttf", "TTF/DejaVuSerif.ttf"],
    ("serif", True, False): ["liberation/LiberationSerif-Bold.ttf", "TTF/DejaVuSerif-Bold.ttf"],
    ("serif", False, True): ["liberation/LiberationSerif-Italic.ttf", "TTF/DejaVuSerif-Italic.ttf"],
    ("sans", False, False): ["liberation/LiberationSans-Regular.ttf", "TTF/DejaVuSans.ttf"],
    ("sans", True, False): ["liberation/LiberationSans-Bold.ttf", "TTF/DejaVuSans-Bold.ttf"],
}

FRENCH_PARAGRAPHS = [
    "Le lendemain matin, nous quittâmes la ville avant l'aube. La route longeait la rivière, "
    "et l'on apercevait déjà les premières maisons du village, serrées autour de l'église.",
    "Il faut avouer que le voyage fut plus long que prévu : les chevaux étaient fatigués, "
    "la pluie avait détrempé les chemins et notre guide hésitait à chaque carrefour.",
    "À midi, nous fîmes halte dans une auberge où l'on nous servit du pain, du fromage "
    "et un vin léger. Le propriétaire, un homme âgé, nous raconta l'histoire de la vallée.",
    "Le soir venu, la brume recouvrit les champs ; seules les cloches de l'abbaye "
    "rappelaient que nous n'étions pas seuls dans cette campagne silencieuse.",
]

GERMAN_PARAGRAPHS = [
    "Die Gemeinde liegt am Fuße der Berge und war über Jahrhunderte ein wichtiger Ort "
    "für den Handel zwischen den Tälern. Die Bewohner lebten vom Ackerbau und vom Weinbau.",
    "Im neunzehnten Jahrhundert veränderte sich das Dorf grundlegend: Straßen wurden gebaut, "
    "eine Schule entstand und die ersten Fabriken öffneten ihre Tore.",
]


@dataclass
class TruthBlock:
    kind: BlockKind
    text: str
    box_px: tuple[int, int, int, int]
    lines: list[str]
    font_size_pt: float
    bold: bool = False
    italic: bool = False
    on_image: bool = False
    column: int = 0


@dataclass
class TruthWord:
    text: str
    box_px: tuple[int, int, int, int]
    block_index: int
    line_index: int
    word_index: int


@dataclass
class TruthImage:
    box_px: tuple[int, int, int, int]
    kind: ImageKind = ImageKind.PHOTO


@dataclass
class SyntheticPage:
    name: str
    image: np.ndarray                                   # RGB uint8
    blocks: list[TruthBlock] = field(default_factory=list)
    words: list[TruthWord] = field(default_factory=list)
    images: list[TruthImage] = field(default_factory=list)
    background_regions_px: list[tuple[int, int, int, int]] = field(default_factory=list)
    category: PageCategory = PageCategory.TEXT
    columns: int = 1
    page_format: PageFormat = A5
    body_size_pt: float = 11.0
    rotation_applied_cw: int = 0            # quarts de tour horaires appliqués à la page droite
    skew_applied_deg: float = 0.0           # rotation antihoraire appliquée (degrés)
    quad: list[tuple[float, float]] | None = None   # coins normalisés de la feuille (tl, tr, br, bl)
    curvature_px: float = 0.0

    @property
    def height(self) -> int:
        return self.image.shape[0]

    @property
    def width(self) -> int:
        return self.image.shape[1]

    @property
    def expected_fix_cw(self) -> int:
        """Quarts de tour horaires à appliquer pour redresser la page."""
        return (4 - self.rotation_applied_cw) % 4

    def box(self, box_px: tuple[int, int, int, int]) -> Box:
        return Box.from_pixels(*box_px, width=self.width, height=self.height)

    def full_text(self) -> str:
        return "\n".join(block.text for block in self.blocks)

    def truth_ocr(self, confidence: float = 95.0, languages: list[str] | None = None) -> OcrResult:
        """OCR « parfait » construit à partir de la vérité terrain (pour tester la mise en page sans Tesseract)."""
        words = [
            OcrWord(text=w.text, box=self.box(w.box_px), confidence=confidence,
                    block_num=w.block_index + 1, par_num=1, line_num=w.line_index + 1, word_num=w.word_index + 1)
            for w in self.words
        ]
        return OcrResult(words=words, width=self.width, height=self.height, languages=languages or ["fra"],
                         engine="truth", engine_version="synthetic")


# --------------------------------------------------------------------------- polices et rendu


@lru_cache(maxsize=None)
def font_path(style: str = "serif", bold: bool = False, italic: bool = False) -> str:
    for relative in FONT_CANDIDATES.get((style, bold, italic), FONT_CANDIDATES[(style, False, False)]):
        candidate = Path("/usr/share/fonts") / relative
        if candidate.exists():
            return str(candidate)
    raise RuntimeError("Aucune police TrueType de test trouvée (Liberation ou DejaVu)")


@lru_cache(maxsize=None)
def _font(size_pt: float, style: str, bold: bool, italic: bool) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(font_path(style, bold, italic), int(round(size_pt * PX_PER_PT)))


def _wrap(text: str, font: ImageFont.FreeTypeFont, width: int, first_indent: int = 0) -> list[str]:
    lines: list[str] = []
    current = ""
    limit = width - first_indent
    for word in text.split():
        trial = f"{current} {word}".strip()
        if font.getlength(trial) <= limit or not current:
            current = trial
        else:
            lines.append(current)
            current = word
            limit = width
    if current:
        lines.append(current)
    return lines


class PageWriter:
    """Écrit des blocs de haut en bas et enregistre la vérité terrain."""

    def __init__(self, page: SyntheticPage, left: int = MARGIN, right: int = PAGE_W - MARGIN, top: int = MARGIN):
        self.page = page
        self.canvas = Image.fromarray(page.image)
        self.draw = ImageDraw.Draw(self.canvas)
        self.left, self.right, self.y = left, right, top

    def finish(self) -> SyntheticPage:
        self.page.image = np.asarray(self.canvas).copy()
        return self.page

    def skip(self, points: float) -> None:
        self.y += int(points * PX_PER_PT)

    def block(self, kind: BlockKind, text: str, size_pt: float, *, bold: bool = False, italic: bool = False,
              style: str = "serif", align: str = "left", indent_pt: float = 0, color=INK, on_image: bool = False,
              column: int = 0, line_spacing: float = 1.2, x_range: tuple[int, int] | None = None,
              y: int | None = None) -> TruthBlock:
        font = _font(size_pt, style, bold, italic)
        left, right = x_range or (self.left, self.right)
        top = self.y if y is None else y
        indent = int(indent_pt * PX_PER_PT)
        lines = _wrap(text, font, right - left, indent)
        line_height = int(round(size_pt * PX_PER_PT * line_spacing))
        block_index = len(self.page.blocks)
        boxes = []
        for line_index, line in enumerate(lines):
            width = font.getlength(line)
            if align == "center":
                x = left + (right - left - width) / 2
            elif align == "right":
                x = right - width
            else:
                x = left + (indent if line_index == 0 else 0)
            y_line = top + line_index * line_height
            self.draw.text((x, y_line), line, font=font, fill=color)
            cursor = x
            for word_index, word in enumerate(line.split(" ")):
                bbox = self.draw.textbbox((cursor, y_line), word, font=font)
                boxes.append(bbox)
                self.page.words.append(TruthWord(word, tuple(int(v) for v in bbox), block_index, line_index, word_index))
                cursor += font.getlength(word + " ")
        box = (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))
        truth = TruthBlock(kind, " ".join(lines), tuple(int(v) for v in box), lines, size_pt, bold, italic,
                           on_image, column)
        self.page.blocks.append(truth)
        if y is None:
            self.y = top + len(lines) * line_height
        return truth

    def picture(self, height: int, x_range: tuple[int, int] | None = None, seed: int = 1) -> TruthImage:
        left, right = x_range or (self.left, self.right)
        photo = photo_texture(right - left, height, seed)
        self.canvas.paste(Image.fromarray(photo), (left, self.y))
        truth = TruthImage((left, self.y, right, self.y + height), ImageKind.PHOTO)
        self.page.images.append(truth)
        self.y += height
        return truth


def blank_page(name: str) -> SyntheticPage:
    image = np.empty((PAGE_H, PAGE_W, 3), np.uint8)
    image[:] = PAPER
    return SyntheticPage(name=name, image=image)


def photo_texture(width: int, height: int, seed: int = 1) -> np.ndarray:
    """Image d'aspect photographique : couleurs saturées, détails, dégradés."""
    rng = np.random.default_rng(seed)
    coarse = rng.integers(0, 255, (6, 8, 3), dtype=np.uint8)
    smooth = cv2.resize(coarse, (width, height), interpolation=cv2.INTER_CUBIC).astype(np.float32)
    detail = rng.normal(0, 18, (height, width, 3)).astype(np.float32)
    ys = np.linspace(0, 1, height, dtype=np.float32)[:, None, None]
    sky = np.array([90, 140, 210], np.float32) * (1 - ys) + np.array([60, 110, 40], np.float32) * ys
    mixed = 0.55 * smooth + 0.45 * sky + detail
    return np.clip(mixed, 0, 255).astype(np.uint8)


# --------------------------------------------------------------------------- pages de référence


def text_page() -> SyntheticPage:
    """Cas A : en-tête, titre, sous-titre, paragraphes accentués, liste, numéro de page."""
    page = blank_page("text_page")
    writer = PageWriter(page)
    writer.block(BlockKind.HEADER, "CHAPITRE PREMIER", 8, style="sans", align="center", y=int(0.045 * PAGE_H))
    writer.y = int(0.11 * PAGE_H)
    writer.block(BlockKind.HEADING_1, "Le voyage vers la vallée", 20, bold=True, align="center")
    writer.skip(8)
    writer.block(BlockKind.HEADING_2, "Où l'on part avant l'aube", 13, italic=True, align="center")
    writer.skip(14)
    for paragraph in FRENCH_PARAGRAPHS[:3]:
        writer.block(BlockKind.PARAGRAPH, paragraph, 11, indent_pt=14)
        writer.skip(6)
    writer.skip(4)
    for item in ["• des provisions pour trois jours ;", "• deux lanternes et de l'huile ;", "• une carte de la région."]:
        writer.block(BlockKind.LIST_ITEM, item, 11, indent_pt=0)
        writer.skip(2)
    writer.block(BlockKind.PAGE_NUMBER, "12", 9, align="center", y=int(0.93 * PAGE_H))
    page.category = PageCategory.TEXT
    return writer.finish()


def german_text_page() -> SyntheticPage:
    page = blank_page("german_text_page")
    writer = PageWriter(page)
    writer.y = int(0.1 * PAGE_H)
    writer.block(BlockKind.HEADING_1, "Geschichte der Gemeinde", 18, bold=True, align="center")
    writer.skip(16)
    for paragraph in GERMAN_PARAGRAPHS:
        writer.block(BlockKind.PARAGRAPH, paragraph, 11, indent_pt=14)
        writer.skip(6)
    writer.block(BlockKind.PAGE_NUMBER, "37", 9, align="center", y=int(0.93 * PAGE_H))
    return writer.finish()


def two_column_page() -> SyntheticPage:
    page = blank_page("two_column_page")
    writer = PageWriter(page)
    writer.y = int(0.08 * PAGE_H)
    writer.block(BlockKind.HEADING_1, "Deux colonnes et un titre", 18, bold=True, align="center")
    writer.skip(18)
    gutter = int(0.04 * PAGE_W)
    middle = (writer.left + writer.right) // 2
    columns = [(writer.left, middle - gutter // 2), (middle + gutter // 2, writer.right)]
    top = writer.y
    for column, x_range in enumerate(columns):
        writer.y = top
        for paragraph in FRENCH_PARAGRAPHS[column * 2: column * 2 + 2]:
            writer.block(BlockKind.PARAGRAPH, paragraph, 10, indent_pt=10, column=column, x_range=x_range)
            writer.skip(8)
    writer.block(BlockKind.PAGE_NUMBER, "13", 9, align="center", y=int(0.93 * PAGE_H))
    page.columns = 2
    return writer.finish()


def graphic_background_page() -> SyntheticPage:
    """Cas B : fond dégradé texturé sur 70 % de la page, texte clair posé dessus, texte courant dessous."""
    page = blank_page("graphic_background_page")
    rng = np.random.default_rng(7)
    height = int(0.70 * PAGE_H)
    ys = np.linspace(0, 1, height, dtype=np.float32)[:, None, None]
    xs = np.linspace(0, 1, PAGE_W, dtype=np.float32)[None, :, None]
    top_colour, bottom_colour = np.array([20, 90, 110], np.float32), np.array([120, 170, 60], np.float32)
    gradient = top_colour * (1 - ys) + bottom_colour * ys + 25 * np.sin(6 * xs + 3 * ys)
    texture = cv2.GaussianBlur(rng.normal(0, 22, (height, PAGE_W)).astype(np.float32), (0, 0), 2)[:, :, None]
    page.image[:height] = np.clip(gradient + texture, 0, 255).astype(np.uint8)
    page.background_regions_px.append((0, 0, PAGE_W, height))
    writer = PageWriter(page)
    writer.y = int(0.18 * PAGE_H)
    writer.block(BlockKind.HEADING_1, "Les saisons du lac", 24, bold=True, style="sans", align="center",
                 color=(255, 255, 255), on_image=True)
    writer.skip(20)
    writer.block(BlockKind.PARAGRAPH, FRENCH_PARAGRAPHS[3], 12, style="sans", color=(255, 255, 240),
                 on_image=True, x_range=(int(0.2 * PAGE_W), int(0.8 * PAGE_W)), align="center")
    writer.y = height + int(0.04 * PAGE_H)
    writer.block(BlockKind.PARAGRAPH, FRENCH_PARAGRAPHS[0], 10, indent_pt=12)
    writer.block(BlockKind.PAGE_NUMBER, "14", 9, align="center", y=int(0.93 * PAGE_H))
    page.category = PageCategory.GRAPHIC_BACKGROUND
    return writer.finish()


def photo_text_page() -> SyntheticPage:
    """Cas C : photographie en haut, légende, puis paragraphes."""
    page = blank_page("photo_text_page")
    writer = PageWriter(page)
    writer.y = int(0.08 * PAGE_H)
    writer.picture(int(0.36 * PAGE_H), seed=3)
    writer.skip(5)
    writer.block(BlockKind.CAPTION, "Vue de la vallée depuis le col, au printemps.", 9, italic=True)
    writer.skip(16)
    for paragraph in FRENCH_PARAGRAPHS[:3]:
        writer.block(BlockKind.PARAGRAPH, paragraph, 11, indent_pt=14)
        writer.skip(6)
    writer.block(BlockKind.PAGE_NUMBER, "15", 9, align="center", y=int(0.93 * PAGE_H))
    page.category = PageCategory.PHOTO_TEXT
    return writer.finish()


# --------------------------------------------------------------------------- transformations


def _copy(page: SyntheticPage, name: str, image: np.ndarray) -> SyntheticPage:
    clone = SyntheticPage(**{**page.__dict__, "name": name, "image": image})
    return clone


def rotated(page: SyntheticPage, quarter_turns_cw: int) -> SyntheticPage:
    """Page tournée de `quarter_turns_cw` quarts de tour horaires (la vérité des boîtes n'est plus valable)."""
    image = np.ascontiguousarray(np.rot90(page.image, k=-quarter_turns_cw))
    clone = _copy(page, f"{page.name}_rot{90 * quarter_turns_cw}", image)
    clone.rotation_applied_cw = quarter_turns_cw % 4
    return clone


def skewed(page: SyntheticPage, angle_deg: float) -> SyntheticPage:
    """Page tournée de `angle_deg` degrés dans le sens antihoraire, coins remplis avec le papier."""
    matrix = cv2.getRotationMatrix2D((page.width / 2, page.height / 2), angle_deg, 1.0)
    image = cv2.warpAffine(page.image, matrix, (page.width, page.height), flags=cv2.INTER_LINEAR,
                           borderMode=cv2.BORDER_CONSTANT, borderValue=PAPER)
    clone = _copy(page, f"{page.name}_skew{angle_deg:+.1f}", image)
    clone.skew_applied_deg = angle_deg
    return clone


def on_dark_background(page: SyntheticPage, jitter: float = 0.04, seed: int = 5) -> SyntheticPage:
    """Feuille photographiée en perspective sur un fond sombre (comme sur un banc de numérisation)."""
    rng = np.random.default_rng(seed)
    canvas_w, canvas_h = int(page.width * 1.6), int(page.height * 1.35)
    base = np.array([[0.2, 0.12], [0.8, 0.12], [0.8, 0.88], [0.2, 0.88]], np.float32)
    corners = base + rng.uniform(-jitter, jitter, base.shape).astype(np.float32)
    destination = corners * np.array([canvas_w, canvas_h], np.float32)
    source = np.array([[0, 0], [page.width, 0], [page.width, page.height], [0, page.height]], np.float32)
    matrix = cv2.getPerspectiveTransform(source, destination)
    canvas = np.empty((canvas_h, canvas_w, 3), np.uint8)
    canvas[:] = (28, 28, 30)
    noise = rng.normal(0, 4, canvas.shape)
    canvas = np.clip(canvas + noise, 0, 255).astype(np.uint8)
    warped = cv2.warpPerspective(page.image, matrix, (canvas_w, canvas_h), dst=canvas,
                                 borderMode=cv2.BORDER_TRANSPARENT)
    clone = _copy(page, f"{page.name}_perspective", warped)
    clone.quad = [(float(x) / canvas_w, float(y) / canvas_h) for x, y in destination]
    return clone


def curved(page: SyntheticPage, amplitude_px: float = 25.0) -> SyntheticPage:
    """Courbure de type reliure : les lignes se courbent (déplacement vertical parabolique)."""
    height, width = page.height, page.width
    xs = np.arange(width, dtype=np.float32)
    offset = amplitude_px * ((xs - width / 2) / (width / 2)) ** 2
    map_x = np.tile(xs, (height, 1))
    map_y = np.arange(height, dtype=np.float32)[:, None] - offset[None, :]
    image = cv2.remap(page.image, map_x, map_y.astype(np.float32), cv2.INTER_LINEAR,
                      borderMode=cv2.BORDER_CONSTANT, borderValue=PAPER)
    clone = _copy(page, f"{page.name}_curved", image)
    clone.curvature_px = amplitude_px
    return clone


def save(page: SyntheticPage, folder: Path, suffix: str = ".png") -> Path:
    """Écrit l'image dans `folder` (jamais dans le dépôt) et renvoie le chemin."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{page.name}{suffix}"
    Image.fromarray(page.image).save(path, dpi=(DPI, DPI), quality=95)
    return path


REFERENCE_PAGES = {
    "text": text_page,
    "graphic": graphic_background_page,
    "photo": photo_text_page,
}
