"""Signalements et rapports produits par les services."""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class Issue(BaseModel):
    """Avertissement ou erreur affichable. `code` est stable (filtrage, tests) ; `message` est en français."""

    code: str
    message: str
    severity: Severity = Severity.WARNING
    page_id: str | None = None
    block_id: str | None = None


class ImportReport(BaseModel):
    added_page_ids: list[str] = Field(default_factory=list)
    issues: list[Issue] = Field(default_factory=list)


class ProgressEvent(BaseModel):
    page_id: str
    page_position: int          # rang 0.. dans le lot
    total: int
    step: str                   # "geometry", "enhance", "ocr", "layout", "images"
    message: str = ""


class ProcessingReport(BaseModel):
    processed: list[str] = Field(default_factory=list)
    failed: dict[str, str] = Field(default_factory=dict)        # page_id -> message utilisateur
    skipped_cached: list[str] = Field(default_factory=list)     # pages dont l'OCR venait du cache
    issues: list[Issue] = Field(default_factory=list)
    duration_s: float = 0.0
    cancelled: bool = False


JobState = Literal["running", "done", "failed", "cancelled", "interrupted"]


class JobStatus(BaseModel):
    job_id: str
    project_id: str
    state: JobState
    page_ids: list[str] = Field(default_factory=list)
    steps: list[str] = Field(default_factory=list)
    done: int = 0
    current_page_id: str | None = None
    current_step: str | None = None
    started_at: datetime
    finished_at: datetime | None = None
    errors: dict[str, str] = Field(default_factory=dict)
    message: str = ""

    @property
    def total(self) -> int:
        return len(self.page_ids)


class TypographyReport(BaseModel):
    pages_affected: int = 0
    blocks_affected: int = 0
    blocks_preserved: int = 0       # blocs dont une surcharge individuelle a été conservée
    preserved_block_ids: list[str] = Field(default_factory=list)


class ExportReport(BaseModel):
    output: str
    format: Literal["docx", "pdf"]
    mode: str
    page_count: int = 0
    text_blocks: int = 0
    images: int = 0
    issues: list[Issue] = Field(default_factory=list)
    fallbacks: list[Issue] = Field(default_factory=list)   # replis (police remplacée, élément simplifié…)
    overflows: list[Issue] = Field(default_factory=list)   # blocs dont le texte ne tient pas dans sa boîte

    @property
    def ok(self) -> bool:
        return not any(issue.severity == Severity.ERROR for issue in self.issues)


class PageValidation(BaseModel):
    page_number: int
    width_pt: float
    height_pt: float
    chars: int
    images: int
    blank: bool
    text_similarity: float | None = None     # 0..1 entre texte attendu et texte relu
    issues: list[Issue] = Field(default_factory=list)


class ValidationReport(BaseModel):
    file: str
    page_count: int
    expected_page_count: int | None = None
    pages: list[PageValidation] = Field(default_factory=list)
    issues: list[Issue] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(issue.severity == Severity.ERROR for issue in self.issues)


class ConversionResult(BaseModel):
    available: bool                  # False si LibreOffice est absent
    output: str | None = None
    duration_s: float = 0.0
    message: str = ""
