"""Numérisation fidèle (« Numérisation intelligente ») : contrat de l'analyse Gemma, réglages, mesures et rapports.

Le PDF de numérisation montre la photographie traitée (jamais une image générée) avec une couche OCR invisible.
Gemma ne fait que proposer : ses réponses sont validées par ces modèles (catégories bornées, liste blanche
d'actions, champs inconnus refusés) puis confrontées aux mesures OpenCV avant toute application.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from models.geometry import Box, PageFormat
from models.reports import Issue

ANALYSIS_REASON_MAX = 400
MAX_CROP = 0.15                         # rognage maximal d'un bord (fraction), page par page ou pour tout le livre
HISTORY_MAX = 30


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- file de traitement


class ScanState(StrEnum):
    """État d'une page dans la file persistante (repositories/scan_repository)."""

    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    FAILED = "FAILED"


# --------------------------------------------------------------------------- contrat Gemma


class ScanAction(StrEnum):
    """Liste blanche des actions qu'une analyse peut recommander. Rien d'autre n'est accepté."""

    ROTATE = "ROTATE"
    DETECT_PAGE = "DETECT_PAGE"
    PERSPECTIVE_CORRECTION = "PERSPECTIVE_CORRECTION"
    SHADOW_CORRECTION = "SHADOW_CORRECTION"
    LIGHTING_CORRECTION = "LIGHTING_CORRECTION"
    CONTRAST_ADJUSTMENT = "CONTRAST_ADJUSTMENT"
    DENOISE = "DENOISE"
    SHARPEN = "SHARPEN"
    PRESERVE_COLOR = "PRESERVE_COLOR"
    MANUAL_REVIEW = "MANUAL_REVIEW"


class PageType(StrEnum):
    TEXT = "text"
    ILLUSTRATED = "illustrated"
    PHOTOGRAPHIC = "photographic"
    MIXED = "mixed"
    BLANK = "blank"
    UNKNOWN = "unknown"


class Level(StrEnum):
    """Intensité d'un défaut visible ; `uncertain` = le modèle ne peut pas trancher."""

    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    UNCERTAIN = "uncertain"


class OrientationIssue(StrEnum):
    NONE = "none"
    ROTATED_90_CW = "rotated_90_cw"
    ROTATED_90_CCW = "rotated_90_ccw"
    UPSIDE_DOWN = "upside_down"
    UNCERTAIN = "uncertain"


class LightingIssue(StrEnum):
    NONE = "none"
    UNEVEN = "uneven"
    UNDEREXPOSED = "underexposed"
    OVEREXPOSED = "overexposed"
    UNCERTAIN = "uncertain"


class ContrastLevel(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    UNCERTAIN = "uncertain"


class ColorContent(StrEnum):
    GRAYSCALE = "grayscale"
    LIMITED_COLOR = "limited_color"
    FULL_COLOR = "full_color"
    UNCERTAIN = "uncertain"


_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class VisualAnalysis(BaseModel):
    """Réponse structurée attendue de Gemma 3 pour une page. Champs inconnus refusés (extra="forbid")."""

    model_config = ConfigDict(extra="forbid")

    page_type: PageType
    orientation_issue: OrientationIssue
    perspective_issue: Level
    crop_issue: Level                     # bords de page visibles, fond autour, recadrage incorrect
    shadow_level: Level
    lighting_issue: LightingIssue
    blur_level: Level
    noise_level: Level
    contrast_level: ContrastLevel
    color_content: ColorContent
    reflection_issue: Level
    curvature_issue: Level
    recommended_actions: list[ScanAction] = Field(default_factory=list, max_length=10)
    manual_review_required: bool
    reason: str = Field(default="", max_length=2000)

    @field_validator("recommended_actions")
    @classmethod
    def _unique_actions(cls, value: list[ScanAction]) -> list[ScanAction]:
        return list(dict.fromkeys(value))

    @field_validator("reason")
    @classmethod
    def _clean_reason(cls, value: str) -> str:
        """Texte affiché seulement (jamais interprété) : caractères de contrôle retirés, longueur bornée."""
        return _CONTROL_CHARS.sub(" ", value).strip()[:ANALYSIS_REASON_MAX]


AnalysisStatus = Literal["ok", "invalid_response", "unavailable", "disabled", "timeout", "error"]


class AnalysisRecord(BaseModel):
    """Résultat d'un appel (réussi ou non) ; mis en cache par empreinte d'image, modèle et version du prompt."""

    status: AnalysisStatus
    analysis: VisualAnalysis | None = None
    model: str = ""
    model_digest: str = ""
    prompt_version: str = ""
    image_sha256: str = ""                 # empreinte des octets réellement envoyés
    image_width: int = 0
    image_height: int = 0
    created_at: datetime = Field(default_factory=utc_now)
    duration_s: float | None = None
    attempts: int = 0
    error: str = ""                        # message utilisateur (français), sans contenu du livre

    @property
    def usable(self) -> bool:
        return self.status == "ok" and self.analysis is not None


# --------------------------------------------------------------------------- réglages et plan


class ScanAdjustments(BaseModel):
    """Réglages photographiques non destructifs appliqués à la page redressée. Valeurs neutres = identité.

    Échelles identiques à `models.page.ImageEnhancement` (−100..100 / 0..100) ; `gamma` > 1 éclaircit les
    tons moyens ; `exposure` multiplie la clarté par 2^(exposure/50) ; `paper_whitening` divise par le fond
    du papier estimé (éclairage uniformisé, papier ramené vers le blanc ; illustrations au gain du papier voisin).
    `crop_*` rognent une fraction de chaque bord après redressement (bande de fond restée au bord).
    Aucune binarisation ni conversion en gris : les couleurs de la photographie sont conservées.
    """

    paper_whitening: float = Field(default=0.0, ge=0, le=100)     # fond du papier uniformisé et blanchi
    background_removal: float = Field(default=0.0, ge=0, le=100)  # fond supprimé (blanc), images conservées
    exposure: float = Field(default=0.0, ge=-30, le=40)
    brightness: float = Field(default=0.0, ge=-50, le=50)
    contrast: float = Field(default=0.0, ge=-50, le=50)
    gamma: float = Field(default=1.0, ge=0.5, le=2.0)
    shadow_correction: float = Field(default=0.0, ge=0, le=80)
    local_contrast: float = Field(default=0.0, ge=0, le=60)        # CLAHE sur la luminance (Lab L)
    denoise: float = Field(default=0.0, ge=0, le=60)
    sharpness: float = Field(default=0.0, ge=0, le=60)
    white_balance: bool = False
    crop_left: float = Field(default=0.0, ge=0.0, le=MAX_CROP)
    crop_top: float = Field(default=0.0, ge=0.0, le=MAX_CROP)
    crop_right: float = Field(default=0.0, ge=0.0, le=MAX_CROP)
    crop_bottom: float = Field(default=0.0, ge=0.0, le=MAX_CROP)

    # Rognage supplémentaire choisi pour tout le livre (ou la page) : s'ajoute au rognage automatique ci-dessus
    # et reste en place quand la page est retraitée.
    extra_left: float = Field(default=0.0, ge=0.0, le=MAX_CROP)
    extra_top: float = Field(default=0.0, ge=0.0, le=MAX_CROP)
    extra_right: float = Field(default=0.0, ge=0.0, le=MAX_CROP)
    extra_bottom: float = Field(default=0.0, ge=0.0, le=MAX_CROP)

    @property
    def has_crop(self) -> bool:
        return any((self.crop_left, self.crop_top, self.crop_right, self.crop_bottom, self.extra_left,
                    self.extra_top, self.extra_right, self.extra_bottom))

    @property
    def extra_trim(self) -> dict[str, float]:
        return {"extra_left": self.extra_left, "extra_top": self.extra_top, "extra_right": self.extra_right,
                "extra_bottom": self.extra_bottom}

    def is_neutral(self) -> bool:
        return self == ScanAdjustments()


AdjustmentSource = Literal["neutral", "measured", "gemma", "manual"]


class AdjustmentPlan(BaseModel):
    """Plan déterministe : chaque action retenue est justifiée par une mesure (et éventuellement par Gemma)."""

    adjustments: ScanAdjustments = Field(default_factory=ScanAdjustments)
    actions: list[ScanAction] = Field(default_factory=list)       # actions retenues (liste blanche)
    rejected_actions: list[ScanAction] = Field(default_factory=list)   # proposées par Gemma, non confirmées
    reasons: list[str] = Field(default_factory=list)
    review_required: bool = False
    review_reasons: list[str] = Field(default_factory=list)        # sous-ensemble de reasons
    source: AdjustmentSource = "measured"


class AdjustmentHistoryEntry(BaseModel):
    at: datetime = Field(default_factory=utc_now)
    source: AdjustmentSource
    adjustments: ScanAdjustments


# --------------------------------------------------------------------------- mesures


class ImageMetrics(BaseModel):
    """Mesures objectives d'une image (copie réduite à 1200 px). Les mesures tonales portent sur
    l'intérieur de la page (bandes sombres de bord exclues) pour ne pas être faussées par le fond.
    """

    width: int
    height: int
    sharpness: float                   # variance du Laplacien (gris)
    contrast: float                    # écart-type de la luminance 0..255
    brightness: float                  # luminance moyenne 0..255
    paper_level: float = 0.0           # 90e centile de la luminance (clarté du papier)
    tonal_range: float = 0.0           # 98e − 2e centile de la luminance
    dark_clip: float                   # part des pixels de luminance <= 5
    bright_clip: float                 # part des pixels de luminance >= 250
    colorfulness: float                # indice de Hasler et Süsstrunk (0 = gris)
    illumination_unevenness: float     # 0..1 : écart relatif du fond papier estimé (ombres, éclairage)
    noise_sigma: float                 # bruit de luminance estimé (Immerkær, recadrage central pleine résolution)
    residual_skew_deg: float | None = None
    skew_reliable: bool = False
    border_content: float = 0.0        # 0..1 : part d'encre dans la bande de bord (contenu peut-être coupé)
    dark_edges: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)   # bandes de fond (g, h, d, b), fraction


class QualityCheck(BaseModel):
    """Comparaison avant (page redressée) / après (page améliorée). Les scores seuls ne prouvent rien."""

    before: ImageMetrics
    after: ImageMetrics
    color_shift: float                 # dérive chromatique moyenne (a*b* de Lab) avant/après, copies réduites
    enhancement_degraded: bool = False   # vrai : l'amélioration est écartée, la page redressée est gardée
    passed: bool = False
    reasons: list[str] = Field(default_factory=list)
    ocr_mean_confidence: float | None = None
    ocr_word_count: int | None = None


# --------------------------------------------------------------------------- état d'une page


FinalSource = Literal["enhanced", "straightened"]


class PageKind(StrEnum):
    """Type de page pour les statistiques et les prix."""

    TEXT = "text"              # texte seul (pas d'image ni de tableau)
    TABLE = "table"            # page avec tableau(x) (grille de traits)
    IMAGE = "image"            # page avec photo(s) ou illustration(s)
    COVER = "cover"            # couverture ou page entièrement illustrée (photo pleine page, fond sombre)


PAGE_KIND_LABELS = {PageKind.TEXT: "Texte seul", PageKind.TABLE: "Avec tableau(x)", PageKind.IMAGE: "Avec image(s)",
                    PageKind.COVER: "Couverture avec image"}


PageOrientation = Literal["auto", "portrait", "landscape"]

# Formats de sortie du PDF de numérisation (distincts des formats de livre détectés pour le DOCX).
SCAN_PAGE_FORMATS: dict[str, PageFormat] = {fmt.name: fmt for fmt in [
    PageFormat(name="A3", width_mm=297, height_mm=420),
    PageFormat(name="A4", width_mm=210, height_mm=297),
    PageFormat(name="A5", width_mm=148, height_mm=210),
    PageFormat(name="B5", width_mm=176, height_mm=250),
    PageFormat(name="Letter", width_mm=215.9, height_mm=279.4),
    PageFormat(name="Legal", width_mm=215.9, height_mm=355.6),
]}
PAGE_FORMAT_IMAGE = "image"             # chaque page PDF a la taille de sa photo finale
PAGE_FORMAT_MEDIAN = "median"           # format commun du livre : taille médiane des pages
PAGE_FORMAT_CUSTOM = "custom"           # largeur × hauteur choisies (mm)


class ScanBookSettings(BaseModel):
    """Réglages de numérisation d'un livre (data/projects/<id>/scan/settings.json)."""

    run_ocr: bool = True                # reconnaissance du texte (couche invisible recherchable du PDF)
    remove_background: bool = True      # fond du papier supprimé (blanc), images conservées
    center_content: bool = True         # dans le PDF, le bloc de contenu est centré sur la page
    page_format: str = PAGE_FORMAT_MEDIAN   # « image », « median », un nom de SCAN_PAGE_FORMATS ou « custom »
    orientation: PageOrientation = "auto"   # auto : chaque page PDF suit le sens de sa photo
    margin_mm: float = Field(default=10.0, ge=0, le=60)
    custom_width_mm: float = Field(default=210.0, ge=50, le=1200)
    custom_height_mm: float = Field(default=297.0, ge=50, le=1200)
    include_unvalidated: bool = True    # le PDF inclut les pages « à vérifier » non validées
    auto_export: bool = True            # PDF créé et enregistré automatiquement à la fin d'un traitement

    @model_validator(mode="before")
    @classmethod
    def _legacy_uniform(cls, data):
        """Ancien réglage `uniform_pages` (booléen) : faux = taille de chaque page, vrai = format commun."""
        if isinstance(data, dict) and "uniform_pages" in data:
            data = dict(data)
            uniform = data.pop("uniform_pages")
            data.setdefault("page_format", PAGE_FORMAT_MEDIAN if uniform else PAGE_FORMAT_IMAGE)
        return data

    @field_validator("page_format")
    @classmethod
    def _known_format(cls, value: str) -> str:
        if value not in {PAGE_FORMAT_IMAGE, PAGE_FORMAT_MEDIAN, PAGE_FORMAT_CUSTOM, *SCAN_PAGE_FORMATS}:
            raise ValueError(f"format de page inconnu : {value}")
        return value

    @property
    def uniform_pages(self) -> bool:
        """Toutes les pages PDF ont le même format (tout sauf « taille de chaque page »)."""
        return self.page_format != PAGE_FORMAT_IMAGE

    @property
    def paper_size_mm(self) -> tuple[float, float] | None:
        """Format papier choisi (largeur, hauteur en portrait), None pour « image » et « median »."""
        if self.page_format == PAGE_FORMAT_CUSTOM:
            return (min(self.custom_width_mm, self.custom_height_mm), max(self.custom_width_mm, self.custom_height_mm))
        fmt = SCAN_PAGE_FORMATS.get(self.page_format)
        return (fmt.width_mm, fmt.height_mm) if fmt else None


class ScanPageRecord(BaseModel):
    """Résultats et paramètres d'une page (data/projects/<id>/scan/<page_id>/record.json).

    L'état de file (PENDING, …) est dans SQLite (repositories/scan_repository) ; ce fichier garde les
    paramètres, l'historique, les mesures et les chemins (relatifs au projet) des images intermédiaires.
    """

    schema_version: int = 1
    page_id: str
    source_sha256: str
    adjustments: ScanAdjustments = Field(default_factory=ScanAdjustments)
    adjustments_source: AdjustmentSource = "neutral"
    proposed_plan: AdjustmentPlan | None = None      # proposition (Gemma + mesures) en attente de validation
    applied_plan: AdjustmentPlan | None = None
    history: list[AdjustmentHistoryEntry] = Field(default_factory=list)
    analysis: AnalysisRecord | None = None
    geometry_issues: list[Issue] = Field(default_factory=list)
    straightened_file: str | None = None             # corrected/<page>.png (partagé avec le pipeline DOCX)
    final_file: str | None = None                    # scan/<page>/final.jpg (seul encodage JPEG, inséré tel quel)
    final_source: FinalSource | None = None
    final_signature: str | None = None               # empreinte géométrie + réglages au moment du rendu
    ocr_file: str | None = None                      # scan/<page>/ocr.json (coordonnées sur final.jpg)
    ocr_signature: str | None = None                 # final_signature au moment de l'OCR
    ocr_skipped: bool = False                        # OCR désactivé pour ce livre lors du rendu : pas de couche texte
    quality: QualityCheck | None = None
    content_box: Box | None = None                   # bloc de contenu (texte + images) sur final.jpg, fond exclu
    picture_share: float | None = None               # part de la page couverte par des images (final.jpg)
    page_kind: PageKind | None = None                # type détecté (texte seul, avec image, couverture)
    page_kind_override: PageKind | None = None       # type choisi par l'utilisateur (prioritaire)
    page_kind_version: int = 0                       # version du classement (reclassement automatique si ancienne)
    review_reasons: list[str] = Field(default_factory=list)
    validated: bool = False                          # validation explicite par l'utilisateur
    error: str | None = None
    timings_s: dict[str, float] = Field(default_factory=dict)
    updated_at: datetime = Field(default_factory=utc_now)

    @property
    def effective_kind(self) -> PageKind | None:
        return self.page_kind_override or self.page_kind

    def remember(self, source: AdjustmentSource, adjustments: ScanAdjustments) -> None:
        """Applique des réglages et garde l'historique (borné) pour pouvoir revenir en arrière."""
        self.history.append(AdjustmentHistoryEntry(source=self.adjustments_source, adjustments=self.adjustments))
        del self.history[:-HISTORY_MAX]
        self.adjustments = adjustments
        self.adjustments_source = source


# --------------------------------------------------------------------------- PDF recherchable


class SearchablePageInput(BaseModel):
    """Une page du PDF : image finale (JPEG, insérée sans recompression) et mots OCR sur cette image."""

    page_id: str
    label: str = ""
    image_path: str
    ocr_path: str | None = None        # OcrResult JSON (models.ocr) ; None = page sans couche texte
    content_box: Box | None = None     # bloc de contenu à centrer (format uniforme, fond blanc) ; None = image entière


class ProbeResult(BaseModel):
    word: str
    page_index: int                    # 0-based
    found: bool


class SearchablePdfReport(BaseModel):
    output: str
    page_count: int = 0
    expected_pages: int = 0
    words_written: int = 0
    pages_without_text: list[int] = Field(default_factory=list)   # indices 0-based
    file_size: int = 0
    dpi: int = 0
    issues: list[Issue] = Field(default_factory=list)


class SearchablePdfValidation(BaseModel):
    path: str
    page_count: int = 0
    expected_pages: int = 0
    page_count_ok: bool = False
    pages_with_text: int = 0
    invisible_text_only: bool = False      # toute la couche texte est en mode de rendu 3 (invisible)
    probes: list[ProbeResult] = Field(default_factory=list)
    images_unrecompressed: bool = False    # le flux image du PDF est identique au JPEG final
    max_color_delta: float | None = None   # ΔE moyen max entre le rendu PDF et l'image finale
    passed: bool = False
    issues: list[Issue] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)


class BookScanStats(BaseModel):
    """Statistiques d'un livre : pages terminées par type (texte seul, avec tableau, avec image, couverture)."""

    book_id: str
    book_name: str
    total_pages: int = 0
    completed_pages: int = 0           # traitement terminé (ou page à vérifier validée)
    review_pages: int = 0
    failed_pages: int = 0
    pending_pages: int = 0
    by_kind: dict[PageKind, int] = Field(default_factory=dict)   # pages terminées, par type effectif


class BookPdfExport(BaseModel):
    """PDF de numérisation enregistré (exports/<nom>.pdf + <nom>.export.json) ; `signature` dit s'il est à jour."""

    pdf_file: str                          # relatif au projet
    created_at: datetime = Field(default_factory=utc_now)
    signature: str                         # empreinte des pages (ordre, images finales, OCR) et des options
    automatic: bool = False                # créé à la fin d'un traitement, sans clic
    settings: ScanBookSettings
    report: SearchablePdfReport
    validation: SearchablePdfValidation


class ValidateAllSummary(BaseModel):
    validated: list[int] = Field(default_factory=list)      # numéros de page validés par cette action
    already: list[int] = Field(default_factory=list)        # déjà validés
    not_ready: list[int] = Field(default_factory=list)      # pas traités, en erreur ou obsolètes : non validés
