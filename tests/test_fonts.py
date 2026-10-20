"""Polices : inventaire, résolution des familles, replis explicites, glyphes manquants."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

import config
from models.errors import ExportError
from services import fonts
from tests.fixtures import synthetic

# Dossier des polices Liberation, quelle que soit la distribution (Arch : liberation/ ; Debian : truetype/liberation/).
_LIBERATION_SERIF = synthetic._installed_fonts().get("LiberationSerif-Regular.ttf")
LIBERATION_DIR = _LIBERATION_SERIF.parent if _LIBERATION_SERIF else Path("/usr/share/fonts/liberation")
requires_liberation = pytest.mark.skipif(_LIBERATION_SERIF is None,
                                         reason="polices Liberation absentes de /usr/share/fonts")


@pytest.fixture
def isolated_fonts(tmp_path, monkeypatch):
    """Dossier de polices réduit à Liberation Serif régulier (cache vidé avant et après)."""
    folder = tmp_path / "fonts"
    folder.mkdir()
    shutil.copy(LIBERATION_DIR / "LiberationSerif-Regular.ttf", folder)
    monkeypatch.setattr(config, "FONT_DIRS", [str(folder)])
    fonts.clear_cache()
    yield folder
    fonts.clear_cache()


@requires_liberation
def test_liberation_families_have_four_variants():
    families = fonts.available_families()
    for name in ("Liberation Serif", "Liberation Sans", "Liberation Mono"):
        family = families[name]
        assert family.regular.name.endswith("-Regular.ttf")
        assert family.bold is not None and family.bold.name.endswith("-Bold.ttf")
        assert family.italic is not None and family.italic.name.endswith("-Italic.ttf")
        assert family.bold_italic is not None and family.bold_italic.name.endswith("-BoldItalic.ttf")


@requires_liberation
def test_read_font_face_uses_name_and_os2_tables():
    face = fonts.read_font_face(LIBERATION_DIR / "LiberationSans-BoldItalic.ttf")
    assert face.family == "Liberation Sans"
    assert face.bold and face.italic
    regular = fonts.read_font_face(LIBERATION_DIR / "LiberationSans-Regular.ttf")
    assert not regular.bold and not regular.italic


def test_read_font_face_rejects_non_font(tmp_path):
    bogus = tmp_path / "faux.ttf"
    bogus.write_bytes(b"ceci n'est pas une police")
    with pytest.raises(ValueError):
        fonts.read_font_face(bogus)


@requires_liberation
def test_resolve_family_exact_case_insensitive_alias_and_default():
    assert fonts.resolve_family("Liberation Serif") == ("Liberation Serif", False)
    assert fonts.resolve_family("liberation sans") == ("Liberation Sans", False)
    # équivalent métrique : repli signalé
    assert fonts.resolve_family("Times New Roman") in {("Liberation Serif", True), ("Times New Roman", False)}
    assert fonts.resolve_family("Police Imaginaire 42") == (config.DEFAULT_FONT_FAMILY, True)


@requires_liberation
def test_font_file_variants():
    assert fonts.font_file("Liberation Serif", False, False).name == "LiberationSerif-Regular.ttf"
    assert fonts.font_file("Liberation Serif", True, False).name == "LiberationSerif-Bold.ttf"
    assert fonts.font_file("Liberation Serif", False, True).name == "LiberationSerif-Italic.ttf"
    assert fonts.font_file("Liberation Serif", True, True).name == "LiberationSerif-BoldItalic.ttf"


@requires_liberation
def test_missing_variant_falls_back_to_regular_and_is_reported(isolated_fonts):
    assert set(fonts.available_families()) == {"Liberation Serif"}
    choice = fonts.choose_font("Liberation Serif", True, True)
    assert choice.path.name == "LiberationSerif-Regular.ttf"
    assert choice.variant_fallback and not choice.family_fallback
    assert fonts.font_file("Liberation Serif", True, False).name == "LiberationSerif-Regular.ttf"
    unknown = fonts.choose_font("Inconnue", False, False)
    assert unknown.family == "Liberation Serif" and unknown.family_fallback


def test_no_font_at_all_raises_explicit_error(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "FONT_DIRS", [str(tmp_path / "vide")])
    fonts.clear_cache()
    try:
        with pytest.raises(ExportError):
            fonts.resolve_family("Liberation Serif")
    finally:
        fonts.clear_cache()


@requires_liberation
def test_missing_glyphs():
    assert fonts.missing_glyphs("Liberation Serif", "Élan, œuvre « déjà » ç à ù ÿ – 12 €") == set()
    assert fonts.missing_glyphs("Liberation Serif", "漢字 abc\n\t") == {"漢", "字"}


@requires_liberation
def test_natural_line_height():
    ratio = fonts.natural_line_height(LIBERATION_DIR / "LiberationSerif-Regular.ttf")
    assert ratio == pytest.approx(1.149, abs=0.01)


def test_natural_line_height_unreadable_file_uses_default(tmp_path):
    bogus = tmp_path / "x.ttf"
    bogus.write_bytes(b"\x00" * 20)
    assert fonts.natural_line_height(bogus) == fonts.DEFAULT_LINE_HEIGHT_RATIO
