"""Client Gemini optionnel. Aucun appel n'est fait tant que GEMINI_ENABLED est faux et que le projet refuse le cloud."""
from __future__ import annotations

import logging

from pydantic import BaseModel

import config
from models import Project
from models.errors import AiUnavailableError

logger = logging.getLogger("bookocr.gemini")

_calls_this_process = 0


class GeminiStatus(BaseModel):
    enabled: bool
    allowed_by_project: bool
    key_configured: bool
    available: bool
    model: str
    calls_this_process: int = 0
    max_calls: int = 0
    message: str = ""


class GeminiUsage(BaseModel):
    calls: int
    max_calls: int
    remaining: int


def _key_configured() -> bool:
    return bool(config.gemini_api_key())


def usage() -> GeminiUsage:
    return GeminiUsage(calls=_calls_this_process, max_calls=config.GEMINI_MAX_CALLS,
                       remaining=max(0, config.GEMINI_MAX_CALLS - _calls_this_process))


def status(project: Project | None = None) -> GeminiStatus:
    allowed = True if project is None else bool(project.cloud_ai_allowed)
    enabled = bool(config.GEMINI_ENABLED)
    key_ok = _key_configured()
    quota = usage()
    if not enabled:
        message = "Gemini est désactivé (GEMINI_ENABLED)."
    elif not allowed:
        message = "Ce projet n'autorise pas l'envoi de pages à une API externe."
    elif not key_ok:
        message = "GEMINI_API_KEY est absente. Aucune requête n'a été envoyée."
    elif quota.remaining <= 0:
        message = "Quota Gemini de ce processus atteint."
    else:
        message = "Gemini est configuré (aucun appel automatique)."
    available = enabled and allowed and key_ok and quota.remaining > 0
    return GeminiStatus(
        enabled=enabled, allowed_by_project=allowed, key_configured=key_ok, available=available,
        model=config.GEMINI_MODEL, calls_this_process=quota.calls, max_calls=quota.max_calls, message=message,
    )


def require_available(project: Project) -> GeminiStatus:
    current = status(project)
    if not current.available:
        raise AiUnavailableError(current.message)
    return current


def register_call() -> None:
    """Comptabilise un appel réel (à n'utiliser que lorsqu'une requête a effectivement été envoyée)."""
    global _calls_this_process
    _calls_this_process += 1
    logger.info("Appel Gemini comptabilisé (%s/%s). Aucune donnée de page n'est journalisée.",
                _calls_this_process, config.GEMINI_MAX_CALLS)
