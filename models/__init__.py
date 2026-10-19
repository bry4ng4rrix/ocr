"""Modèles de données (Pydantic v2). Ce paquet n'importe rien d'autre du projet."""
from models.analysis import (CurvatureResult, DetectedRegion, LayoutResult, LayoutSettings, OrientationResult,
                             QuadResult, SkewResult)
from models.blocks import (HEADING_KINDS, MARGINAL_KINDS, Alignment, BlockKind, ImageBlock, ImageKind, OcrLine,
                           TextBlock, TextStyle, new_id)
from models.composition import (ColumnBand, ComposedPage, ImageElement, Margins, Rect, ResolvedStyle, TextElement)
from models.geometry import MM_TO_PT, STANDARD_FORMATS, Box, PageFormat, Point
from models.ocr import OcrResult, OcrWord
from models.page import GeometryCorrection, ImageEnhancement, Page, PageCategory, PageStatus
from models.project import (MODE_LABELS, SCHEMA_VERSION, ExportSettings, Project, ProjectSummary,
                            ReconstructionMode, TypographySettings)
from models.quality import (BookQuality, ImageQualityMetrics, PageQuality, RouteBand,
                            VisualComparisonResult)
from models.reports import (ConversionResult, ExportReport, ImportReport, Issue, JobStatus, PageValidation,
                            ProcessingReport, ProgressEvent, Severity, TypographyReport, ValidationReport)

__all__ = [name for name in dir() if not name.startswith("_")]
