
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = 1
HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
EPS = 1e-6


def new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


# --------------------------------------------------------------------------- geometry primitives

class Box(_Model):
    """A rectangle in normalized frame coordinates (0..1, origin top-left, y downwards)."""
    x: float = Field(0.0, ge=0.0, le=1.0)
    y: float = Field(0.0, ge=0.0, le=1.0)
    width: float = Field(1.0, gt=0.0, le=1.0)
    height: float = Field(1.0, gt=0.0, le=1.0)

    @model_validator(mode="after")
    def _inside(self):
        if self.x + self.width > 1.0 + 1e-4 or self.y + self.height > 1.0 + 1e-4:
            raise ValueError("la boîte dépasse du cadre de la page (x + largeur ou y + hauteur > 1)")
        return self

    @property
    def x2(self) -> float:
        return self.x + self.width

    @property
    def y2(self) -> float:
        return self.y + self.height

    @property
    def area(self) -> float:
        return self.width * self.height

    @classmethod
    def clamped(cls, x: float, y: float, width: float, height: float, min_size: float = 0.002) -> "Box":
        """Build a box forced inside the frame (used by editors and detectors)."""
        width = min(max(width, min_size), 1.0)
        height = min(max(height, min_size), 1.0)
        x = min(max(x, 0.0), 1.0 - width)
        y = min(max(y, 0.0), 1.0 - height)
        return cls(x=x, y=y, width=width, height=height)


Point = tuple[float, float]


# --------------------------------------------------------------------------- image settings

class WhiteBalance(str, Enum):
    NONE = "none"
    GRAY_WORLD = "gray_world"     # average colour of the image made neutral
    WHITE_PATCH = "white_patch"   # brightest pixels (paper) made white


class Binarization(str, Enum):
    NONE = "none"
    OTSU = "otsu"
    SAUVOLA = "sauvola"


class ImageAdjustments(_Model):
    """Photometric corrections, applied in a fixed order from a reference image (never cumulated).

    Every default is neutral: `apply_adjustments(img, ImageAdjustments())` returns `img` unchanged.
    `enabled=False` turns the whole set off without losing the values.
    Order: white balance, exposure, temperature/tint, shadows/highlights, brightness, contrast,
    gamma, saturation/hue, illumination flattening, local contrast (CLAHE), denoise, sharpen,
    grayscale, binarization.
    """
    enabled: bool = True
    exposure: float = Field(0.0, ge=-3.0, le=3.0)           # EV, gain 2**exposure in linear light
    brightness: float = Field(0.0, ge=-100.0, le=100.0)     # offset on lightness
    contrast: float = Field(0.0, ge=-100.0, le=100.0)       # around mid-grey
    gamma: float = Field(1.0, ge=0.2, le=5.0)               # >1 brightens mid-tones
    saturation: float = Field(0.0, ge=-100.0, le=100.0)     # -100 = grey
    hue: float = Field(0.0, ge=-180.0, le=180.0)            # degrees
    temperature: float = Field(0.0, ge=-100.0, le=100.0)    # >0 warmer (more red, less blue)
    tint: float = Field(0.0, ge=-100.0, le=100.0)           # >0 magenta, <0 green
    white_balance: WhiteBalance = WhiteBalance.NONE
    shadows: float = Field(0.0, ge=-100.0, le=100.0)        # >0 lifts dark tones
    highlights: float = Field(0.0, ge=-100.0, le=100.0)     # <0 recovers bright tones
    sharpness: float = Field(0.0, ge=0.0, le=300.0)         # unsharp mask amount, percent
    sharpen_radius: float = Field(1.0, ge=0.3, le=10.0)     # pixels
    denoise: float = Field(0.0, ge=0.0, le=30.0)            # non-local means strength
    local_contrast: float = Field(0.0, ge=0.0, le=8.0)      # CLAHE clip limit, 0 = off
    illumination_correction: bool = False                   # flatten uneven lighting / shadows
    grayscale: bool = False
    binarization: Binarization = Binarization.NONE

    def is_neutral(self) -> bool:
        return not self.enabled or self == ImageAdjustments(enabled=self.enabled)


class AdjustmentTarget(str, Enum):
    PRINT = "print"   # the colour image used for the facsimile and the illustrations
    OCR = "ocr"       # the image read by the OCR
    BOTH = "both"


class LocalAdjustment(_Model):
    """Corrections limited to a rectangle of the frame (applied after the global ones)."""
    id: str = Field(default_factory=lambda: new_id("loc_"))
    label: str = ""
    box: Box
    adjustments: ImageAdjustments = Field(default_factory=ImageAdjustments)
    target: AdjustmentTarget = AdjustmentTarget.PRINT
    restore_original: bool = False    # the region shows the geometry frame without any correction
    feather: float = Field(0.0, ge=0.0, le=0.2)   # soft edge, as a fraction of the region's short side
    enabled: bool = True


class GeometrySettings(_Model):
    """How the page frame is built from the original image. Changing it moves the coordinates.

    Steps, in order: EXIF orientation (always), quarter turns (`rotation` = user turn +
    detected orientation), page cut-out (manual quad, else detected quad when `auto_crop`),
    fine rotation (manual `deskew_angle`, else detected skew when `auto_deskew`), margin trim
    (percent of each side), then `scale_percent` resize.
    """
    rotation: Literal[0, 90, 180, 270] = 0                  # manual clockwise quarter turns
    auto_orientation: bool = True
    detected_orientation: Literal[0, 90, 180, 270] | None = None   # filled by the processing
    auto_crop: bool = True
    manual_quad: list[Point] | None = None                  # tl, tr, br, bl as 0..1 of the turned original
    detected_quad: list[Point] | None = None
    auto_deskew: bool = True
    deskew_angle: float | None = Field(None, ge=-45.0, le=45.0)   # manual angle, degrees (counter-clockwise)
    detected_skew: float | None = None
    trim_left: float = Field(0.0, ge=0.0, le=40.0)          # percent of the width / height
    trim_top: float = Field(0.0, ge=0.0, le=40.0)
    trim_right: float = Field(0.0, ge=0.0, le=40.0)
    trim_bottom: float = Field(0.0, ge=0.0, le=40.0)
    auto_trim_margins: bool = False                          # cut empty outer margins (keeps a padding)
    scale_percent: float = Field(100.0, ge=10.0, le=400.0)

    @field_validator("manual_quad", "detected_quad")
    @classmethod
    def _quad(cls, quad):
        if quad is None:
            return None
        if len(quad) != 4 or any(not (-1e-3 <= v <= 1 + 1e-3) for p in quad for v in p):
            raise ValueError("un quadrilatère doit avoir 4 coins en coordonnées 0..1")
        return [(float(x), float(y)) for x, y in quad]

    def effective_rotation(self) -> int:
        detected = self.detected_orientation if (self.auto_orientation and self.detected_orientation) else 0
        return (self.rotation + detected) % 360

    def effective_quad(self) -> list[Point] | None:
        return self.manual_quad or (self.detected_quad if self.auto_crop else None)

    def effective_skew(self) -> float:
        if self.deskew_angle is not None:
            return self.deskew_angle
        return (self.detected_skew or 0.0) if self.auto_deskew else 0.0

    def signature(self) -> str:
        """Changes whenever the frame (hence the coordinate system) changes. Scale is excluded:
        normalized coordinates do not depend on the resolution."""
        quad = self.effective_quad()
        raw = json.dumps({
            "turn": self.effective_rotation(),
            "quad": None if quad is None else [round(v, 4) for p in quad for v in p],
            "skew": round(self.effective_skew(), 2),
            "trim": [round(v, 2) for v in (self.trim_left, self.trim_top, self.trim_right, self.trim_bottom)],
            "auto_trim": self.auto_trim_margins,
        }, sort_keys=True)
        return hashlib.sha1(raw.encode()).hexdigest()[:16]


class ProcessingMode(str, Enum):
    AUTO = "auto"                  # chosen from the page content
    TEXT = "text"                  # mode A: OCR readability first
    ILLUSTRATION = "illustration"  # mode B: colours and details first
    MIXED = "mixed"                # mode C: OCR treatment on text regions, colour treatment on illustrations


# --------------------------------------------------------------------------- blocks

class BlockType(str, Enum):
    TITLE = "title"
    SUBTITLE = "subtitle"
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST = "list"
    CAPTION = "caption"
    HEADER = "header"
    FOOTER = "footer"
    PAGE_NUMBER = "page_number"
    TABLE = "table"
    ILLUSTRATION = "illustration"
    DECORATION = "decoration"
    OTHER = "other"


TEXT_TYPES = {BlockType.TITLE, BlockType.SUBTITLE, BlockType.HEADING, BlockType.PARAGRAPH, BlockType.LIST,
              BlockType.CAPTION, BlockType.HEADER, BlockType.FOOTER, BlockType.PAGE_NUMBER, BlockType.TABLE,
              BlockType.OTHER}
IMAGE_TYPES = {BlockType.ILLUSTRATION, BlockType.DECORATION, BlockType.TABLE, BlockType.OTHER}

BLOCK_LABELS = {
    BlockType.TITLE: "Titre", BlockType.SUBTITLE: "Sous-titre", BlockType.HEADING: "Intertitre",
    BlockType.PARAGRAPH: "Paragraphe", BlockType.LIST: "Liste", BlockType.CAPTION: "Légende",
    BlockType.HEADER: "En-tête", BlockType.FOOTER: "Pied de page", BlockType.PAGE_NUMBER: "Numéro de page",
    BlockType.TABLE: "Tableau", BlockType.ILLUSTRATION: "Illustration", BlockType.DECORATION: "Décoration",
    BlockType.OTHER: "Autre",
}


class Alignment(str, Enum):
    LEFT = "left"
    RIGHT = "right"
    CENTER = "center"
    JUSTIFY = "justify"


class OCRWord(_Model):
    """A recognised word. Like OCRLine, its box is in normalized frame coordinates (0..1, top-left
    origin), never in pixels: the facsimile text layer and the editor rely on it."""
    text: str
    x: float
    y: float
    width: float
    height: float
    confidence: float = Field(-1.0, ge=-1.0, le=100.0)   # -1: not given by the engine


class OCRLine(_Model):
    x: float
    y: float
    width: float
    height: float
    words: list[OCRWord] = Field(default_factory=list)

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)


class _BlockBase(_Model):
    id: str = Field(default_factory=lambda: new_id("b_"))
    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)
    width: float = Field(gt=0.0, le=1.0)
    height: float = Field(gt=0.0, le=1.0)
    rotation: float = Field(0.0, ge=-360.0, le=360.0)   # degrees, counter-clockwise, around the box centre
    reading_order: int = Field(0, ge=0)
    column: int = Field(0, ge=0)                         # 0-based column index found by the layout analysis
    confidence: float = Field(-1.0, ge=-1.0, le=100.0)  # detection / OCR confidence, -1 unknown
    needs_review: bool = False                           # low confidence or uncertain detection
    review_reason: str = ""
    manual: bool = False                                 # created or moved by the user
    hidden: bool = False                                 # kept in the project, left out of the PDF

    @model_validator(mode="after")
    def _inside(self):
        if self.x + self.width > 1.0 + 1e-4 or self.y + self.height > 1.0 + 1e-4:
            raise ValueError("le bloc dépasse du cadre de la page")
        return self

    @property
    def box(self) -> Box:
        return Box(x=self.x, y=self.y, width=self.width, height=self.height)

    def set_box(self, box: Box) -> None:
        # Assign through model_validate to keep the "inside the frame" rule atomic.
        data = self.model_dump()
        data.update(x=box.x, y=box.y, width=box.width, height=box.height)
        checked = type(self).model_validate(data)
        for key in ("x", "y", "width", "height"):
            object.__setattr__(self, key, getattr(checked, key))


class TextBlock(_BlockBase):
    kind: Literal["text"] = "text"
    type: BlockType = BlockType.PARAGRAPH
    text: str = ""                    # current text (corrected or not): what the PDF shows
    ocr_text: str = ""                # as read by the engine, never overwritten by user edits
    language: str = ""
    font_family: str = "Serif"       # generic ("Serif", "Sans", "Mono") or a font family name
    font_size: float = Field(11.0, ge=1.0, le=300.0)   # points, for a page of Page.font_reference_height_pt
    font_weight: Literal["normal", "bold"] = "normal"
    italic: bool = False
    underline: bool = False
    color: str = "#000000"
    alignment: Alignment = Alignment.LEFT
    line_spacing: float = Field(1.2, ge=0.5, le=4.0)   # multiple of the font size
    letter_spacing: float = Field(0.0, ge=-2.0, le=20.0)   # points
    paragraph_spacing: float = Field(0.0, ge=0.0, le=72.0)  # points between paragraphs of the block
    first_line_indent: float = Field(0.0, ge=0.0, le=144.0)  # points
    keep_line_breaks: bool = False    # True: every OCR line break is kept (poems, lists, tables)
    lines: list[OCRLine] = Field(default_factory=list)
    low_confidence_words: list[str] = Field(default_factory=list)
    estimated: dict = Field(default_factory=dict)   # raw style measurements, for information only

    @field_validator("color")
    @classmethod
    def _color(cls, value: str) -> str:
        if not HEX_COLOR.match(value):
            raise ValueError("couleur attendue au format #rrggbb")
        return value.lower()

    @property
    def is_corrected(self) -> bool:
        return self.text != self.ocr_text


class ImageBlock(_BlockBase):
    kind: Literal["image"] = "image"
    type: BlockType = BlockType.ILLUSTRATION
    image_path: str                   # current source image, relative to the project folder (lossless PNG)
    original_image_path: str = ""    # first extraction, kept for "revert"
    preserve_aspect_ratio: bool = True
    crop: Box = Field(default_factory=Box)          # part of the source image that is shown
    adjustments: ImageAdjustments = Field(default_factory=ImageAdjustments)   # validated: used in the PDF
    draft_adjustments: ImageAdjustments | None = None   # being edited, not used in the PDF until validated
    source_width: int = Field(0, ge=0)                # pixels of the source image
    source_height: int = Field(0, ge=0)
    detection: str = ""                               # "photo", "drawing", "table", "manual", "replaced"...
    caption_id: str | None = None                     # TextBlock id of its caption
    source_box: Box | None = None                     # frame region the source was cut from (None: uploaded image)

    @property
    def has_unvalidated_edits(self) -> bool:
        return self.draft_adjustments is not None and self.draft_adjustments != self.adjustments


Block = Annotated[Union[TextBlock, ImageBlock], Field(discriminator="kind")]


# --------------------------------------------------------------------------- pages and project

class PageStatus(str, Enum):
    IMPORTED = "imported"
    PROCESSED = "processed"     # OCR + layout done
    ERROR = "error"


class Margins(_Model):
    """Empty space around the content, fractions of the frame."""
    left: float = 0.0
    top: float = 0.0
    right: float = 0.0
    bottom: float = 0.0


class Page(_Model):
    id: str = Field(default_factory=lambda: new_id("p_"))
    original_path: str                      # relative to the project folder, never modified
    original_filename: str = ""
    source_index: int = 0                   # frame of a TIFF / page of a PDF the image came from
    original_width: int = Field(0, ge=0)
    original_height: int = Field(0, ge=0)
    sha256: str = ""
    geometry: GeometrySettings = Field(default_factory=GeometrySettings)
    processing_mode: ProcessingMode = ProcessingMode.AUTO
    resolved_mode: ProcessingMode | None = None      # what AUTO resolved to at the last processing
    ocr_adjustments: ImageAdjustments = Field(default_factory=ImageAdjustments)
    print_adjustments: ImageAdjustments = Field(default_factory=ImageAdjustments)
    local_adjustments: list[LocalAdjustment] = Field(default_factory=list)
    # Products of the processing (relative paths, regenerable from the settings above).
    frame_path: str = ""
    ocr_image_path: str = ""
    frame_width: int = Field(0, ge=0)
    frame_height: int = Field(0, ge=0)
    thumbnail_path: str = ""
    # OCR and layout results.
    blocks: list[Block] = Field(default_factory=list)
    ocr_geometry_signature: str = ""        # GeometrySettings.signature() when the OCR ran
    ocr_languages: list[str] = Field(default_factory=list)
    ocr_mean_confidence: float = -1.0
    ocr_preparation: str = ""               # which OCR image gave the result ("gris", "binarisé")
    columns: int = Field(1, ge=0)
    margins: Margins = Field(default_factory=Margins)
    printed_page_number: str = ""
    font_reference_height_pt: float = Field(0.0, ge=0.0)   # page height the font sizes refer to
    status: PageStatus = PageStatus.IMPORTED
    warnings: list[str] = Field(default_factory=list)
    error: str = ""
    processed_at: datetime | None = None

    @property
    def text_blocks(self) -> list[TextBlock]:
        return [b for b in self.blocks if isinstance(b, TextBlock)]

    @property
    def image_blocks(self) -> list[ImageBlock]:
        return [b for b in self.blocks if isinstance(b, ImageBlock)]

    @property
    def ocr_outdated(self) -> bool:
        """The frame changed since the OCR: block coordinates no longer match the image."""
        return bool(self.ocr_geometry_signature) and self.ocr_geometry_signature != self.geometry.signature()

    def block(self, block_id: str) -> TextBlock | ImageBlock | None:
        return next((b for b in self.blocks if b.id == block_id), None)

    def ordered_blocks(self) -> list[TextBlock | ImageBlock]:
        return sorted(self.blocks, key=lambda b: (b.reading_order, b.y, b.x))


class PageSize(str, Enum):
    AUTO = "auto"       # frame pixels / render.auto_dpi
    A4 = "A4"
    A5 = "A5"
    LETTER = "Letter"
    CUSTOM = "custom"


PAGE_SIZES_MM = {PageSize.A4: (210.0, 297.0), PageSize.A5: (148.0, 210.0), PageSize.LETTER: (215.9, 279.4)}


class OverflowPolicy(str, Enum):
    SHRINK_THEN_EXTEND = "shrink_then_extend"   # smaller font (down to min_font_scale), then free space below, then clip + warn
    SHRINK = "shrink"                           # smaller font, then clip + warn
    CLIP = "clip"                               # keep the size, clip + warn


class RenderSettings(_Model):
    page_size: PageSize = PageSize.AUTO
    custom_width_mm: float = Field(210.0, ge=20.0, le=2000.0)
    custom_height_mm: float = Field(297.0, ge=20.0, le=2000.0)
    auto_dpi: float = Field(300.0, ge=50.0, le=1200.0)
    margin_mm: float = Field(0.0, ge=0.0, le=100.0)   # fixed formats: white border around the scaled frame
    orientation_follows_page: bool = True               # fixed formats turn landscape for landscape frames
    overflow_policy: OverflowPolicy = OverflowPolicy.SHRINK_THEN_EXTEND
    min_font_scale: float = Field(0.6, ge=0.2, le=1.0)
    default_font_family: str = "Serif"
    scale_fonts_with_page: bool = True                 # font sizes follow the page height ratio
    image_dpi: int = Field(300, ge=72, le=1200)        # illustrations are never embedded above this
    jpeg_quality: int = Field(0, ge=0, le=100)         # 0: lossless (PNG/Flate) illustrations
    facsimile_text_layer: bool = True                   # mode A: invisible OCR text over the page image
    include_hidden_blocks: bool = False
    title: str = ""
    author: str = ""


class ProcessingSettings(_Model):
    languages: list[str] = Field(default_factory=lambda: ["fra", "eng"])
    default_mode: ProcessingMode = ProcessingMode.AUTO
    auto_orientation: bool = True
    auto_crop: bool = True
    auto_deskew: bool = True
    detect_illustrations: bool = True
    retry_binarized: bool = True        # a second, binarized reading when confidence is low
    dehyphenate: bool = True            # join words cut at the end of a line ("exem-\nple")
    low_confidence: float = Field(60.0, ge=0.0, le=100.0)


class Project(_Model):
    schema_version: int = SCHEMA_VERSION
    id: str = Field(default_factory=lambda: new_id("prj_"))
    name: str = Field("Nouveau livre", min_length=1, max_length=200)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    pages: list[Page] = Field(default_factory=list)
    processing: ProcessingSettings = Field(default_factory=ProcessingSettings)
    render: RenderSettings = Field(default_factory=RenderSettings)

    @field_validator("schema_version")
    @classmethod
    def _version(cls, value: int) -> int:
        if value > SCHEMA_VERSION:
            raise ValueError(f"projet créé par une version plus récente (format {value}, pris en charge : {SCHEMA_VERSION})")
        return value

    def page(self, page_id: str) -> Page | None:
        return next((p for p in self.pages if p.id == page_id), None)

    def page_index(self, page_id: str) -> int:
        for i, p in enumerate(self.pages):
            if p.id == page_id:
                return i
        raise KeyError(page_id)

    def to_json(self) -> str:
        return self.model_dump_json(indent=2)

    @classmethod
    def from_json(cls, raw: str | bytes) -> "Project":
        return cls.model_validate_json(raw)
