"""Intégration avec le VRAI modèle local (Ollama + gemma3:4b). Facultatif et lent (≈ 1 à 2 min par page sur CPU).

Lancement : BOOKOCR_TEST_OLLAMA=1 .venv/bin/python -m pytest -q -m ollama tests/test_ollama_integration.py
Sans cette variable, ou si le serveur ou le modèle est absent, les tests sont ignorés (jamais de téléchargement).
"""
from __future__ import annotations

import os

import pytest

from models.scanning import ScanAction, VisualAnalysis
from services import image_analysis_service, ollama_service
from tests.fixtures import synthetic

pytestmark = [pytest.mark.ollama, pytest.mark.slow,
              pytest.mark.skipif(os.environ.get("BOOKOCR_TEST_OLLAMA") != "1",
                                 reason="intégration Ollama réelle désactivée (BOOKOCR_TEST_OLLAMA=1 pour l'activer)")]


@pytest.fixture
def real_ollama(monkeypatch):
    monkeypatch.setattr("config.OLLAMA_ENABLED", True)
    monkeypatch.setattr("config.OLLAMA_ANALYSIS_ENABLED", True)
    monkeypatch.setattr("config.OLLAMA_TIMEOUT", 300)
    monkeypatch.setattr("config.OLLAMA_MAX_RETRIES", 0)
    status = ollama_service.status()
    if not status.available:
        pytest.skip(status.message)
    info = ollama_service.model_details()
    if not info.has_vision:
        pytest.skip("le modèle configuré n'a pas la capacité vision")
    image_analysis_service._digest_memo.clear()
    return info


def test_real_gemma_returns_a_valid_structured_analysis(real_ollama, tmp_path):
    page = synthetic.skewed(synthetic.text_page(), 4.0).image
    record = image_analysis_service.analyse_page(page, tmp_path, force=True)
    assert record.status == "ok", record.error
    assert isinstance(record.analysis, VisualAnalysis)
    assert all(isinstance(action, ScanAction) for action in record.analysis.recommended_actions)
    assert record.model_digest == real_ollama.digest
    cached = image_analysis_service.analyse_page(page, tmp_path)
    assert cached.created_at == record.created_at                 # servi par le cache, aucun nouvel appel
