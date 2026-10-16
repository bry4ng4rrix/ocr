"""Errors with a message meant for the user (French), raised by the core modules and services."""


class LivreOCRError(Exception):
    """Base class: str(error) is shown as is in the interface."""


class InvalidFileError(LivreOCRError):
    """Unreadable, unsupported, too large or corrupted file."""


class OCRUnavailableError(LivreOCRError):
    """Tesseract missing, or a requested language model is not installed."""


class OCRFailedError(LivreOCRError):
    """Tesseract ran but failed (timeout, crash)."""


class ProcessingError(LivreOCRError):
    """A processing step failed on a page (page detection, extraction...)."""


class PDFGenerationError(LivreOCRError):
    """The PDF could not be written."""


class ProjectError(LivreOCRError):
    """Invalid project file, unsafe path, missing workspace file."""
