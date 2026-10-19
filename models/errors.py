"""Exceptions métier. Chaque exception porte un message destiné à l'utilisateur, en français."""
from __future__ import annotations


class BookOcrError(Exception):
    """Base des erreurs de l'application. `user_message` est affichable tel quel dans l'interface."""

    def __init__(self, user_message: str, *, detail: str = "") -> None:
        super().__init__(user_message if not detail else f"{user_message} ({detail})")
        self.user_message = user_message
        self.detail = detail


class ProjectNotFoundError(BookOcrError):
    pass


class ProjectCorruptedError(BookOcrError):
    pass


class PageNotFoundError(BookOcrError):
    pass


class BlockNotFoundError(BookOcrError):
    pass


class UnsupportedFileError(BookOcrError):
    pass


class FileTooLargeError(BookOcrError):
    pass


class ImageReadError(BookOcrError):
    pass


class ProtectedPathError(BookOcrError):
    """Tentative d'écriture dans le dossier des originaux."""


class OcrUnavailableError(BookOcrError):
    """Tesseract ou une langue demandée est absent."""


class OcrFailedError(BookOcrError):
    pass


class ProcessingError(BookOcrError):
    pass


class JobAlreadyRunningError(BookOcrError):
    pass


class ExportError(BookOcrError):
    pass


class ConversionUnavailableError(BookOcrError):
    """LibreOffice (soffice) absent : conversion DOCX -> PDF impossible."""


class ImportPathError(BookOcrError):
    """Chemin d'import local invalide ou illisible."""


class AiUnavailableError(BookOcrError):
    """Ollama ou Gemini désactivé, absent, ou refusé par la confidentialité du projet."""


class AiTimeoutError(AiUnavailableError):
    """Délai dépassé lors d'un appel au modèle local."""


class AiResponseError(BookOcrError):
    """Réponse du modèle absente, mal formée ou hors du schéma attendu."""
