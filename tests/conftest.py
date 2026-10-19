"""Fixtures communes : dossier de données isolé, pages synthétiques, photos réelles copiées."""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

os.environ.setdefault("BOOKOCR_SKIP_DOTENV", "1")   # un .env local ne doit pas changer les tests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.fixtures import synthetic  # noqa: E402

REAL_PAGES_DIR = Path(os.environ.get("BOOKOCR_REAL_PAGES", "/home/garrix/Dev/LivreOCR_Web/media/books/2026/10/pages"))


def tesseract_languages() -> set[str]:
    if shutil.which("tesseract") is None:
        return set()
    done = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True, timeout=30)
    return {line.strip() for line in done.stdout.splitlines()[1:] if line.strip()}


TESSERACT_LANGS = tesseract_languages()
HAS_SOFFICE = shutil.which(os.environ.get("BOOKOCR_SOFFICE", "soffice")) is not None


def requires_languages(*languages: str):
    missing = [lang for lang in languages if lang not in TESSERACT_LANGS]
    return pytest.mark.skipif(bool(missing), reason=f"Tesseract ou langue(s) absente(s) : {missing or 'tesseract'}")


requires_soffice = pytest.mark.skipif(not HAS_SOFFICE, reason="LibreOffice (soffice) absent")


@pytest.fixture
def data_dir(tmp_path, monkeypatch) -> Path:
    """Redirige toutes les données de l'application vers un dossier temporaire."""
    folder = tmp_path / "data"
    folder.mkdir()
    monkeypatch.setenv("BOOKOCR_DATA_DIR", str(folder))
    return folder


@pytest.fixture(scope="session")
def text_page():
    return synthetic.text_page()


@pytest.fixture(scope="session")
def two_column_page():
    return synthetic.two_column_page()


@pytest.fixture(scope="session")
def graphic_page():
    return synthetic.graphic_background_page()


@pytest.fixture(scope="session")
def photo_page():
    return synthetic.photo_text_page()


@pytest.fixture(scope="session", params=["text", "graphic", "photo"])
def reference_page(request):
    """Les trois cas obligatoires du cahier des charges."""
    return synthetic.REFERENCE_PAGES[request.param]()


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def real_pages(tmp_path) -> list[Path]:
    """Copie des photos réelles dans tmp_path ; vérifie après le test que les originaux n'ont pas changé."""
    if not REAL_PAGES_DIR.is_dir():
        pytest.skip(f"photos réelles absentes : {REAL_PAGES_DIR}")
    sources = sorted(REAL_PAGES_DIR.glob("*.jpg"))
    before = {path: sha256(path) for path in sources}
    target = tmp_path / "real"
    target.mkdir()
    copies = [Path(shutil.copy2(path, target / path.name)) for path in sources]
    yield copies
    after = {path: sha256(path) for path in sources}
    assert before == after, "une photo réelle d'origine a été modifiée"
