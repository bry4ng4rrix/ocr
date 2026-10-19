"""Analyse locale Ollama à la demande ; les réponses restent des réglages bornés."""
from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request

from pydantic import BaseModel, Field, ValidationError

import config


class VisionAdvice(BaseModel):
    shadows: int = Field(ge=0, le=60)
    contrast: int = Field(ge=0, le=35)
    brightness: int = Field(ge=-15, le=15)
    sharpness: int = Field(ge=0, le=35)
    color_document: bool
    illustrations: bool
    description: str = Field(max_length=300)


def _request(path: str, payload: dict | None = None, timeout: int = 5) -> dict:
    request = urllib.request.Request(config.OLLAMA_BASE_URL + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Content-Type": "application/json"}, method="POST" if payload is not None else "GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        value = json.loads(response.read().decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Réponse Ollama invalide.")
    return value


def test_connection(timeout: int = 4) -> str:
    """Vérifie serveur puis modèle configuré, sans déclencher de téléchargement."""
    payload = _request("/api/tags", timeout=timeout)
    models = [m.get("name", "") for m in payload.get("models", []) if isinstance(m, dict)]
    if not any(config.OLLAMA_VISION_MODEL == name or name.startswith(config.OLLAMA_VISION_MODEL + ":")
               for name in models):
        raise RuntimeError(f"Modèle « {config.OLLAMA_VISION_MODEL} » absent ; aucun téléchargement effectué.")
    return f"Ollama disponible, modèle {config.OLLAMA_VISION_MODEL}."


def analyze(rgb_bytes: bytes, timeout: int = 35) -> VisionAdvice:
    """Envoie une image uniquement au serveur Ollama local et valide strictement son JSON."""
    prompt = ('Analyse cette page de livre et retourne uniquement un JSON: '
              '{"shadows":0..60,"contrast":0..35,"brightness":-15..15,"sharpness":0..35,'
              '"color_document":true/false,"illustrations":true/false,"description":"court"}.')
    response = _request("/api/generate", {"model": config.OLLAMA_VISION_MODEL, "stream": False,
        "format": "json", "prompt": prompt, "images": [base64.b64encode(rgb_bytes).decode("ascii")]}, timeout)
    try:
        return VisionAdvice.model_validate_json(response["response"])
    except (KeyError, ValidationError, ValueError, TypeError) as error:
        raise ValueError("La réponse de Gemma ne respecte pas le schéma de recommandations.") from error
