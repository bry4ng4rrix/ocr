"""Ollama et analyse Gemma, sans modèle chargé : faux serveur HTTP local (tests/fixtures/fake_ollama.py).

Couvre : disponibilité, modèle absent, réponse multimodale valide, réponse invalide, délai dépassé,
serveur indisponible, nouvelles tentatives bornées, cache, liste blanche des actions, IA désactivée.
"""
from __future__ import annotations

import base64
import json

import numpy as np
import pytest

from models.errors import AiResponseError, AiTimeoutError, AiUnavailableError
from models.scanning import ScanAction, VisualAnalysis
from services import image_analysis_service, ollama_service
from tests.fixtures.fake_ollama import VALID_ANALYSIS, ChatReply, FakeOllama, free_port_url, start


@pytest.fixture
def fake(monkeypatch):
    fake = FakeOllama()
    server, _ = start(fake)
    monkeypatch.setattr("config.OLLAMA_ENABLED", True)
    monkeypatch.setattr("config.OLLAMA_ANALYSIS_ENABLED", True)
    monkeypatch.setattr("config.OLLAMA_BASE_URL", fake.url)
    monkeypatch.setattr("config.OLLAMA_VISION_MODEL", "gemma3:4b")
    monkeypatch.setattr("config.OLLAMA_TIMEOUT", 5)
    monkeypatch.setattr("config.OLLAMA_MAX_RETRIES", 2)
    image_analysis_service._digest_memo.clear()
    yield fake
    server.shutdown()
    server.server_close()


def _page() -> np.ndarray:
    rng = np.random.default_rng(3)
    return rng.integers(150, 255, (1200, 900, 3), dtype=np.uint8)


def _chats(fake: FakeOllama) -> list[dict]:
    return [payload for path, payload in fake.requests if path == "/api/chat"]


# --------------------------------------------------------------------------- 1, 2, 6 : disponibilité


def test_ollama_available_with_vision_model(fake):
    status = ollama_service.status()
    assert status.available and status.vision_model == "gemma3:4b"
    info = ollama_service.model_details()
    assert info.has_vision and info.digest == "digest-gemma3:4b"


def test_missing_model_is_reported_and_never_downloaded(fake, monkeypatch, tmp_path):
    monkeypatch.setattr("config.OLLAMA_VISION_MODEL", "gemma3:12b")
    status = ollama_service.status()
    assert not status.available and "n'est pas installé" in status.message
    with pytest.raises(AiUnavailableError, match="n'est pas installé"):
        ollama_service.model_details()
    assert not any(path in ("/api/pull", "/api/create") for path, _ in fake.requests)
    record = image_analysis_service.analyse_page(_page(), tmp_path / "cache")
    assert record.status == "unavailable" and not record.usable
    assert not (tmp_path / "cache").exists()


def test_server_down_does_not_raise_in_analysis(monkeypatch, tmp_path):
    monkeypatch.setattr("config.OLLAMA_ENABLED", True)
    monkeypatch.setattr("config.OLLAMA_ANALYSIS_ENABLED", True)
    monkeypatch.setattr("config.OLLAMA_BASE_URL", free_port_url())
    image_analysis_service._digest_memo.clear()
    assert ollama_service.status().available is False
    record = image_analysis_service.analyse_page(_page(), tmp_path)
    assert record.status == "unavailable"
    assert "ne répond pas" in record.error


def test_chat_on_down_server_retries_then_raises(monkeypatch):
    monkeypatch.setattr("config.OLLAMA_ENABLED", True)
    monkeypatch.setattr("config.OLLAMA_BASE_URL", free_port_url())
    with pytest.raises(AiUnavailableError):
        ollama_service.chat_json("p", b"jpeg", {}, retries=1, timeout=2)


def test_disabled_ai_makes_no_request(fake, monkeypatch, tmp_path):
    monkeypatch.setattr("config.OLLAMA_ENABLED", False)
    record = image_analysis_service.analyse_page(_page(), tmp_path)
    assert record.status == "disabled"
    assert fake.requests == []
    with pytest.raises(AiUnavailableError):
        ollama_service.chat_json("p", b"x", {})


# --------------------------------------------------------------------------- 3 : réponse valide


def test_valid_multimodal_response_is_parsed_and_cached(fake, tmp_path):
    record = image_analysis_service.analyse_page(_page(), tmp_path)
    assert record.status == "ok" and record.usable
    assert record.analysis.recommended_actions == [ScanAction.SHADOW_CORRECTION]
    assert record.model == "gemma3:4b" and record.model_digest == "digest-gemma3:4b"
    assert record.image_width <= 896 and record.image_height <= 896
    chats = _chats(fake)
    assert len(chats) == 1
    payload = chats[0]
    assert payload["stream"] is False and payload["options"]["temperature"] == 0
    assert len(payload["messages"][0]["images"]) == 1                       # une seule page par requête
    assert base64.b64decode(payload["messages"][0]["images"][0])[:2] == b"\xff\xd8"   # JPEG
    assert set(payload["format"]["required"]) == set(VisualAnalysis.model_fields)
    # même image, même modèle : pas de nouvel appel
    again = image_analysis_service.analyse_page(_page(), tmp_path)
    assert again.status == "ok" and len(_chats(fake)) == 1
    forced = image_analysis_service.analyse_page(_page(), tmp_path, force=True)
    assert forced.status == "ok" and len(_chats(fake)) == 2


def test_cache_key_changes_with_model_digest(fake, tmp_path):
    image_analysis_service.analyse_page(_page(), tmp_path)
    fake.models = ["gemma3:4b"]
    image_analysis_service._digest_memo["gemma3:4b"] = (1e18, "autre-digest")
    image_analysis_service.analyse_page(_page(), tmp_path)
    assert len(_chats(fake)) == 2


# --------------------------------------------------------------------------- 4 : réponse invalide


@pytest.mark.parametrize("content", [
    "pas du JSON",
    json.dumps({**VALID_ANALYSIS, "recommended_actions": ["rm -rf /"]}),          # hors liste blanche
    json.dumps({**VALID_ANALYSIS, "shadow_level": "extreme"}),                    # catégorie inconnue
    json.dumps({**VALID_ANALYSIS, "python": "import os"}),                        # champ inconnu
    json.dumps({k: v for k, v in VALID_ANALYSIS.items() if k != "page_type"}),    # champ manquant
])
def test_invalid_gemma_response_gives_cautious_status(fake, tmp_path, content):
    fake.chat = lambda payload: ChatReply(content=content)
    record = image_analysis_service.analyse_page(_page(), tmp_path)
    assert record.status == "invalid_response"
    assert record.analysis is None and not record.usable
    assert "schéma" in record.error


def test_malformed_http_body_raises_response_error(fake):
    fake.chat = lambda payload: ChatReply(raw_body=b"{not json")
    with pytest.raises(AiResponseError):
        ollama_service.chat_json("p", b"jpeg", {})


def test_reason_is_cleaned_and_bounded():
    analysis = VisualAnalysis.model_validate({**VALID_ANALYSIS, "reason": "a\x00b" + "x" * 1500})
    assert "\x00" not in analysis.reason and len(analysis.reason) <= 400
    with pytest.raises(ValueError):
        VisualAnalysis.model_validate({**VALID_ANALYSIS, "reason": "x" * 2500})
    duplicated = VisualAnalysis.model_validate({**VALID_ANALYSIS, "recommended_actions": ["DENOISE", "DENOISE"]})
    assert duplicated.recommended_actions == [ScanAction.DENOISE]


# --------------------------------------------------------------------------- 5 : délai dépassé, tentatives


def test_timeout_is_retried_a_bounded_number_of_times(fake, monkeypatch, tmp_path):
    monkeypatch.setattr("config.OLLAMA_TIMEOUT", 1)
    monkeypatch.setattr("config.OLLAMA_MAX_RETRIES", 1)
    fake.chat = lambda payload: ChatReply(delay_s=2.5)
    record = image_analysis_service.analyse_page(_page(), tmp_path)
    assert record.status == "timeout"
    assert len(_chats(fake)) == 2                     # 1 essai + 1 nouvelle tentative, pas plus
    assert not list(tmp_path.glob("*.json"))          # un délai dépassé n'est pas mis en cache
    with pytest.raises(AiTimeoutError):
        ollama_service.chat_json("p", b"jpeg", {}, retries=0, timeout=1)


def test_server_error_is_retried_then_succeeds(fake):
    calls = {"n": 0}

    def flaky(payload):
        calls["n"] += 1
        return ChatReply(status=503) if calls["n"] == 1 else ChatReply()

    fake.chat = flaky
    result = ollama_service.chat_json("p", b"jpeg", {}, retries=2)
    assert result.attempts == 2


def test_client_error_is_not_retried(fake):
    fake.chat = lambda payload: ChatReply(status=400)
    with pytest.raises(AiUnavailableError, match="HTTP 400"):
        ollama_service.chat_json("p", b"jpeg", {}, retries=3)
    assert len(_chats(fake)) == 1
