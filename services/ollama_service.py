"""Client Ollama optionnel. L'application fonctionne sans ce service (désactivé par défaut)."""
from __future__ import annotations

import base64
import json
import logging
import socket
import threading
import time
import urllib.error
import urllib.request

from pydantic import BaseModel, Field

import config
from models.errors import AiResponseError, AiTimeoutError, AiUnavailableError, BookOcrError

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


def preferred_vision_model(models: list[str]) -> str:
    """Choisit le modèle vision disponible, avec un comportement sûr et reproductible."""
    configured = (config.OLLAMA_VISION_MODEL or "").strip()
    if configured:
        return configured

    preferred_names = ["gemma3:4b", "gemma3", "llama3.2-vision", "llava", "qwen2.5vl"]
    for preferred in preferred_names:
        if any(name == preferred or name.startswith(f"{preferred}:") or name.startswith(preferred) for name in models):
            return preferred
    return models[0] if models else ""


def status() -> OllamaStatus:
    if not is_enabled():
        return OllamaStatus(enabled=False, available=False, base_url=config.OLLAMA_BASE_URL,
                            message="Ollama est désactivé (OLLAMA_ENABLED).")
    try:
        models = list_models()
    except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError) as error:
        logger.info("Ollama indisponible : %s", type(error).__name__)
        return OllamaStatus(enabled=True, available=False, base_url=config.OLLAMA_BASE_URL,
                            vision_model=(config.OLLAMA_VISION_MODEL or "").strip(),
                            message="Le serveur Ollama ne répond pas. Le pipeline local continue.")
    vision = preferred_vision_model(models)
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


# --------------------------------------------------------------------------- requêtes multimodales

_request_slots = threading.BoundedSemaphore(max(1, config.OLLAMA_MAX_PARALLEL_REQUESTS))
_RETRYABLE_HTTP = {500, 502, 503, 504}


class OllamaModelInfo(BaseModel):
    name: str
    digest: str = ""
    capabilities: list[str] = Field(default_factory=list)
    parameter_size: str = ""
    quantization: str = ""

    @property
    def has_vision(self) -> bool:
        return "vision" in self.capabilities


class OllamaChatResult(BaseModel):
    content: str                       # texte JSON renvoyé par le modèle (non encore validé)
    model: str
    duration_s: float
    attempts: int
    eval_count: int | None = None
    prompt_eval_count: int | None = None


def configured_model() -> str:
    return (config.OLLAMA_VISION_MODEL or "").strip()


def _post_json(path: str, payload: dict, timeout: float) -> dict:
    url = f"{config.OLLAMA_BASE_URL}{path}"
    request = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        value = json.loads(response.read().decode("utf-8"))
    if not isinstance(value, dict):
        raise AiResponseError("Réponse Ollama inattendue (objet JSON attendu).")
    return value


def _http_error_message(error: urllib.error.HTTPError) -> str:
    try:
        detail = json.loads(error.read().decode("utf-8")).get("error", "")
    except (OSError, ValueError, AttributeError):
        detail = ""
    return str(detail)[:200]


def model_details(model: str | None = None, timeout: float = 5.0) -> OllamaModelInfo:
    """Lit /api/show (digest, capacités). Lève AiUnavailableError si le modèle n'est pas installé."""
    name = model or configured_model()
    try:
        payload = _post_json("/api/show", {"model": name}, timeout)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise AiUnavailableError(f"Le modèle « {name} » n'est pas installé dans Ollama "
                                     "(aucun téléchargement n'est lancé).") from error
        raise AiUnavailableError(f"Ollama a refusé la requête (HTTP {error.code}).") from error
    except (OSError, TimeoutError, ValueError) as error:
        raise AiUnavailableError("Le serveur Ollama ne répond pas.", detail=type(error).__name__) from error
    details = payload.get("details") or {}
    return OllamaModelInfo(
        name=name, digest=_digest_from_tags(name),
        capabilities=[str(c) for c in payload.get("capabilities") or [] if isinstance(c, str)],
        parameter_size=str(details.get("parameter_size", "")), quantization=str(details.get("quantization_level", "")),
    )


def _digest_from_tags(name: str) -> str:
    """Le digest n'est exposé que par /api/tags : il identifie la version exacte du modèle (clé de cache)."""
    try:
        payload = _get_json("/api/tags", 3.0)
    except (OSError, TimeoutError, ValueError):
        return ""
    for item in payload.get("models") or []:
        if isinstance(item, dict) and item.get("name") == name:
            return str(item.get("digest", ""))
    return ""


def chat_json(prompt: str, image_jpeg: bytes, schema: dict, *, model: str | None = None,
              timeout: float | None = None, retries: int | None = None) -> OllamaChatResult:
    """Envoie UNE image et une consigne au serveur Ollama local ; la sortie est contrainte par `schema`.

    Une seule requête à la fois par défaut (OLLAMA_MAX_PARALLEL_REQUESTS). Nouvelles tentatives bornées
    seulement pour un délai dépassé, une erreur réseau ou une erreur serveur 5xx. Rien n'est journalisé
    du contenu de la page ni de la réponse.
    """
    if not is_enabled():
        raise AiUnavailableError("Ollama est désactivé (OLLAMA_ENABLED=false).")
    name = model or configured_model()
    if not name:
        raise AiUnavailableError("Aucun modèle Ollama n'est configuré (OLLAMA_VISION_MODEL).")
    wait = float(timeout if timeout is not None else config.OLLAMA_TIMEOUT)
    max_attempts = 1 + (config.OLLAMA_MAX_RETRIES if retries is None else max(0, retries))
    payload = {
        "model": name, "stream": False, "format": schema, "options": {"temperature": 0},
        "messages": [{"role": "user", "content": prompt,
                      "images": [base64.b64encode(image_jpeg).decode("ascii")]}],
    }
    last_error: BookOcrError | None = None
    for attempt in range(1, max_attempts + 1):
        started = time.monotonic()
        try:
            with _request_slots:
                response = _post_json("/api/chat", payload, wait)
        except urllib.error.HTTPError as error:
            message = _http_error_message(error)
            logger.warning("Ollama HTTP %s (tentative %d/%d)", error.code, attempt, max_attempts)
            if error.code == 404:
                raise AiUnavailableError(f"Le modèle « {name} » n'est pas installé dans Ollama "
                                         "(aucun téléchargement n'est lancé).", detail=message) from error
            if error.code not in _RETRYABLE_HTTP:
                raise AiUnavailableError(f"Ollama a refusé la requête (HTTP {error.code}).", detail=message) from error
            last_error = AiUnavailableError(f"Erreur du serveur Ollama (HTTP {error.code}).", detail=message)
        except (TimeoutError, socket.timeout) as error:
            logger.warning("Ollama : délai de %.0f s dépassé (tentative %d/%d)", wait, attempt, max_attempts)
            last_error = AiTimeoutError(f"Gemma n'a pas répondu en {wait:.0f} s.")
        except urllib.error.URLError as error:
            if isinstance(error.reason, (TimeoutError, socket.timeout)):
                logger.warning("Ollama : délai dépassé (tentative %d/%d)", attempt, max_attempts)
                last_error = AiTimeoutError(f"Gemma n'a pas répondu en {wait:.0f} s.")
            else:
                logger.warning("Ollama injoignable (tentative %d/%d) : %s", attempt, max_attempts,
                               type(error.reason).__name__)
                last_error = AiUnavailableError("Le serveur Ollama ne répond pas.",
                                                detail=type(error.reason).__name__)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise AiResponseError("Ollama a renvoyé une réponse illisible.") from error
        except OSError as error:
            logger.warning("Ollama : erreur réseau (tentative %d/%d) : %s", attempt, max_attempts,
                           type(error).__name__)
            last_error = AiUnavailableError("Le serveur Ollama ne répond pas.", detail=type(error).__name__)
        else:
            message = response.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if not isinstance(content, str) or not content.strip():
                raise AiResponseError("La réponse de Gemma est vide.")
            return OllamaChatResult(
                content=content, model=str(response.get("model", name)),
                duration_s=round(time.monotonic() - started, 2), attempts=attempt,
                eval_count=response.get("eval_count"), prompt_eval_count=response.get("prompt_eval_count"),
            )
    assert last_error is not None
    raise last_error
