import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TESSERACT = shutil.which("tesseract")


def _languages() -> set[str]:
    if not TESSERACT:
        return set()
    import subprocess
    out = subprocess.run([TESSERACT, "--list-langs"], capture_output=True, text=True).stdout
    return {line.strip() for line in out.splitlines()[1:] if line.strip()}


LANGS = _languages()
requires_tesseract = pytest.mark.skipif(not {"fra", "eng"} <= LANGS,
                                        reason="Tesseract avec fra et eng requis")


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """An isolated workspace folder for services that write to disk."""
    import config
    ws = tmp_path / "workspace"
    monkeypatch.setattr(config, "WORKSPACE_DIR", ws)
    monkeypatch.setattr(config, "PROJECTS_DIR", ws / "projects")
    monkeypatch.setattr(config, "LOG_DIR", ws / "logs")
    return ws
