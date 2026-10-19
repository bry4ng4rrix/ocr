"""Client Ollama optionnel. L'application fonctionne sans ce service (désactivé par défaut)."""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

from pydantic import BaseModel, Field

import config
from models.errors import AiUnavailableError

logger = logging.getLogger("bookocr.ollama")


class OllamaStatus(BaseModel):
    enabled: bool
    available: bool
    base_url: str
    models: list[str] = Field(default_factory=list)
    vision_model: str = ""
    message: str = ""


def is_enabled() -> bool:
    return bool(config.OLLAMA_ENABLED)


def _get_json(path: str, timeout: float) -> dict:
    url = f"{config.OLLAMA_BASE_URL}{path}"
    request = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def list_models(timeout: float = 2.0) -> list[str]:
    payload = _get_json("/api/tags", timeout)
    models = payload.get("models") or []
    names = []
    for item in models:
        if isinstance(item, dict) and item.get("name"):
            names.append(str(item["name"]))
    return names


def status() -> OllamaStatus:
    if not is_enabled():
        return OllamaStatus(enabled=False, available=False, base_url=config.OLLAMA_BASE_URL,
                            message="Ollama est désactivé (OLLAMA_ENABLED).")
    try:
        models = list_models()
    except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError) as error:
        logger.info("Ollama indisponible : %s", type(error).__name__)
        return OllamaStatus(enabled=True, available=False, base_url=config.OLLAMA_BASE_URL,
                            vision_model=config.OLLAMA_VISION_MODEL,
                            message="Le serveur Ollama ne répond pas. Le pipeline local continue.")
    vision = config.OLLAMA_VISION_MODEL
    compatible = (not vision) or any(vision == name or name.startswith(f"{vision}:") or name.startswith(vision)
                                     for name in models)
    if vision and not compatible:
        return OllamaStatus(enabled=True, available=False, base_url=config.OLLAMA_BASE_URL, models=models,
                            vision_model=vision,
                            message=f"Le modèle « {vision} » n'est pas installé. Aucun téléchargement n'a été lancé.")
    return OllamaStatus(enabled=True, available=True, base_url=config.OLLAMA_BASE_URL, models=models,
                        vision_model=vision, message="Ollama est joignable.")


def require_available() -> OllamaStatus:
    current = status()
    if not current.available:
        raise AiUnavailableError(current.message or "Ollama n'est pas disponible.")
    return current
