"""Export PDF : rendu direct PyMuPDF, fac-similé, aperçu, conversion LibreOffice, validation (relecture réelle)."""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import numpy as np
import pymupdf
import pytest

import config
from models import BlockKind, ComposedPage, Rect, ReconstructionMode, Severity, TextElement
from services import pdf_service
from services.document_service import build_docx
from services.pdf_service import (convert_docx_to_pdf, font_scales, paragraph_html, pdf_page_count,
                                  rasterize_pdf_page, render_pdf, render_preview, style_css, validate_pdf)
from tests.conftest import requires_soffice
from tests.test_document_service import (ALL_MODES, PAGE_H, PAGE_W, PARAGRAPHS, STYLES, book, case_a, case_b,
                                         case_c, make_images, make_source_image, project, two_columns, unanalysed)

ROOT = Path(__file__).resolve().parents[1]
CHECK_PDF = ROOT / ".claude/skills/pdf-export-validation/scripts/check_pdf.py"


@pytest.fixture(params=ALL_MODES, ids=[mode.value for mode in ALL_MODES])
def rendered(request, tmp_path):
    mode = request.param
    pages = book(mode, tmp_path)
    output = tmp_path / f"livre_{mode.value}.pdf"
    report = render_pdf(pages, output)
    return mode, pages, output, report


def small_box_page(text_height: float) -> ComposedPage:
    """Un paragraphe long dans une boîte de hauteur imposée."""
    page = case_a(ReconstructionMode.FIDELITY)
    element = TextElement(block_id="serre", kind=BlockKind.PARAGRAPH, text=" ".join(PARAGRAPHS[:2]),
                          rect=Rect(x_pt=45, y_pt=300, width_pt=200, height_pt=text_height),
                          style=STYLES[BlockKind.PARAGRAPH], positioning="absolute")
    page.elements = [element]
    return page


# --------------------------------------------------------------------------- exigences 18, 19, 22


def test_page_count(rendered):
    _mode, pages, output, report = rendered
    assert report.page_count == len(pages)
    with pymupdf.open(output) as document:
        assert document.page_count == len(pages)
        for pdf_page in document:
            assert pdf_page.rect.width == pytest.approx(PAGE_W, abs=0.01)
            assert pdf_page.rect.height == pytest.approx(PAGE_H, abs=0.01)
    assert pdf_page_count(output) == len(pages)


def test_direct_pdf_text_and_images(rendered):
    _mode, pages, output, report = rendered
    assert report.ok, [issue.message for issue in report.issues if issue.severity == Severity.ERROR]
    assert report.text_blocks == sum(len(page.text_elements) for page in pages)
    assert report.images == sum(len(page.backgrounds) + len(page.image_elements) for page in pages)
    validation = validate_pdf(output, pages)
    assert validation.ok, [issue.message for issue in validation.issues]
    for page, page_report in zip(pages, validation.pages):
        if page.analysed:
            assert page_report.chars > 0
            assert page_report.text_similarity is not None and page_report.text_similarity >= 0.95
            assert page_report.images == len({i.file for i in [*page.backgrounds, *page.image_elements]})
    with pymupdf.open(output) as document:
        text = document[0].get_text()
        assert "Chapitre premier" in text and "fromage" in text and "« tempête »" in text
        assert "Les Couleurs du Sud" in document[1].get_text()


def test_fonts_are_embedded_and_subset(rendered):
    _mode, _pages, output, _report = rendered
    with pymupdf.open(output) as document:
        fonts_used = {font for pdf_page in document for font in pdf_page.get_fonts(full=True)}
    assert fonts_used
    assert all(font[1] != "n/a" for font in fonts_used), "police non intégrée"
    names = {font[3] for font in fonts_used}
    assert any("Liberation Serif" in name for name in names) and any("Liberation Sans" in name for name in names)
    assert all("+" in name for name in names), f"polices non sous-ensemblées : {names}"
    _mode, pages, _output, _report = rendered
    variants = {(e.style.font_family, e.style.bold, e.style.italic) for page in pages for e in page.text_elements}
    xrefs = {font[0] for font in fonts_used}
    assert len(xrefs) == len(variants), "une seule copie intégrée par variante (garbage=4)"
    with pymupdf.open(output) as document:
        font_bytes = sum(len(document.extract_font(xref)[3]) for xref in xrefs)
    assert font_bytes < 50_000 * len(variants), f"{font_bytes} octets de polices : sous-ensemble attendu"



def test_no_blank_pages(rendered):
    _mode, pages, output, report = rendered
    validation = validate_pdf(output, pages)
    blank = [page_report.page_number for page_report in validation.pages if page_report.blank]
    assert blank == [5], "seule la page non analysée est blanche"
    assert any(issue.code == "page_not_analysed" for issue in report.issues)
    assert not [issue for issue in validation.issues if issue.code == "blank_page"]


def test_unanalysed_page_never_shows_photo(tmp_path):
    page = unanalysed(ReconstructionMode.FIDELITY)
    images = make_images(tmp_path / "images")
    page.source_image = str(images["photo"])
    output = tmp_path / "vide.pdf"
    report = render_pdf([page], output, searchable_facsimile=True)
    with pymupdf.open(output) as document:
        assert document[0].get_images() == [] and document[0].get_text() == ""
    assert any(issue.code == "page_not_analysed" for issue in report.issues)


# --------------------------------------------------------------------------- exigence 21 (débordements)


def test_overflow_detected_and_text_kept(tmp_path):
    page = small_box_page(text_height=20)
    for policy in ("shrink", "report"):
        output = tmp_path / f"debordement_{policy}.pdf"
        report = render_pdf([page], output, overflow_policy=policy)
        assert len(report.overflows) == 1, policy
        overflow = report.overflows[0]
        assert overflow.block_id == "serre" and overflow.severity == Severity.WARNING
        assert "dépasse" in overflow.message
        with pymupdf.open(output) as document:
            text = " ".join(document[0].get_text().split())
        assert "fatigués" in text and "carrefour." in text, "le texte débordant n'est jamais perdu"


def test_moderate_overflow_is_shrunk_within_box(tmp_path):
    page = small_box_page(text_height=150)
    measured = render_pdf([page], tmp_path / "mesure.pdf", overflow_policy="report")
    assert measured.overflows == []                    # 150 pt suffisent à taille réelle (≈ 111 pt mesurés)
    page = small_box_page(text_height=90)
    output = tmp_path / "reduit.pdf"
    report = render_pdf([page], output, overflow_policy="shrink")
    assert len(report.overflows) == 1 and report.overflows[0].severity == Severity.INFO
    assert "réduit à" in report.overflows[0].message
    with pymupdf.open(output) as document:
        words = document[0].get_text("words")
        sizes = {round(span["size"], 1) for block in document[0].get_text("dict")["blocks"]
                 for line in block["lines"] for span in line["spans"]}
    assert max(word[3] for word in words) <= 300 + 90 + 1, "le texte reste dans sa boîte"
    assert all(10.5 * 0.7 - 0.1 <= size < 10.5 for size in sizes), sizes


def test_font_scales_stop_at_seventy_percent():
    scales = font_scales()
    assert scales[0] == pytest.approx(0.97) and scales[-1] == 0.7
    assert scales == sorted(scales, reverse=True)


# --------------------------------------------------------------------------- fac-similé, aperçu


def test_searchable_facsimile(tmp_path):
    images = make_images(tmp_path / "images")
    pages = [case_a(ReconstructionMode.FIDELITY), case_c(ReconstructionMode.FIDELITY, images)]
    for page in pages:
        page.source_image = str(make_source_image(page, tmp_path / "sources" / f"{page.page_id}.png"))
    output = tmp_path / "facsimile.pdf"
    report = render_pdf(pages, output, searchable_facsimile=True)
    assert report.ok and report.mode == "facsimile" and report.images == 2
    validation = validate_pdf(output, pages, searchable_facsimile=True)
    assert validation.ok, [issue.message for issue in validation.issues]
    with pymupdf.open(output) as document:
        for pdf_page, page_report in zip(document, validation.pages):
            assert page_report.images == 1
            assert page_report.text_similarity >= 0.9
            spans = pdf_page.get_texttrace()
            assert spans and all(span["type"] == 3 for span in spans), "texte invisible (render_mode 3)"
        assert document[0].search_for("Chapitre premier")


def test_facsimile_without_image_falls_back_and_reports(tmp_path):
    page = case_a(ReconstructionMode.FIDELITY)
    report = render_pdf([page], tmp_path / "sans_image.pdf", searchable_facsimile=True)
    assert any(issue.code == "facsimile_image_missing" for issue in report.fallbacks)
    assert validate_pdf(tmp_path / "sans_image.pdf", [page]).ok


def test_render_preview_png_not_empty(tmp_path):
    images = make_images(tmp_path / "images")
    image, issues = render_preview(case_b(ReconstructionMode.FIDELITY, images), width_px=900)
    assert image.mode == "RGB" and image.width == 900
    assert image.height == round(900 * PAGE_H / PAGE_W) or abs(image.height - 900 * PAGE_H / PAGE_W) <= 1
    pixels = np.asarray(image)
    assert (pixels < 235).any(axis=2).mean() > 0.3          # fond vert visible
    assert pixels[: image.height // 2].std() > 10
    assert all(issue.severity != Severity.ERROR for issue in issues)
    image.save(tmp_path / "apercu.png")
    assert (tmp_path / "apercu.png").stat().st_size > 1000


def test_background_fills_its_rect_even_with_other_ratio(tmp_path):
    """Non-régression : un fond au ratio légèrement différent ne laisse pas de bande blanche (comme l'ancre DOCX)."""
    images = make_images(tmp_path / "images")
    narrow = tmp_path / "images" / "fond_etroit.png"
    from PIL import Image
    Image.open(images["background"]).resize((300, 417)).save(narrow)
    page = case_b(ReconstructionMode.FIDELITY, images)
    page.backgrounds[0].file = str(narrow)
    image, _issues = render_preview(page, width_px=420)
    pixels = np.asarray(image)
    for x in (3, 416):
        assert pixels[100, x].tolist() != [255, 255, 255], f"bande blanche en x={x}"


def test_preview_matches_pdf_page(tmp_path):
    """L'aperçu et le PDF viennent du même code de rendu (D-08)."""
    page = case_a(ReconstructionMode.EDITABLE)
    preview, _issues = render_preview(page, width_px=600)
    render_pdf([page], tmp_path / "a.pdf")
    from_pdf = rasterize_pdf_page(tmp_path / "a.pdf", 0, width_px=600)
    assert preview.size == from_pdf.size
    difference = np.abs(np.asarray(preview, dtype=np.int16) - np.asarray(from_pdf, dtype=np.int16))
    assert difference.mean() < 1.0


def test_rasterize_pdf_page_and_bounds(tmp_path):
    pages = [case_a(ReconstructionMode.EDITABLE, 1), case_a(ReconstructionMode.EDITABLE, 2)]
    output = tmp_path / "deux.pdf"
    render_pdf(pages, output)
    assert pdf_page_count(output) == 2
    image = rasterize_pdf_page(output, 1, width_px=350)
    assert image.mode == "RGB" and image.width == 350 and (np.asarray(image) < 200).any()
    with pytest.raises(IndexError):
        rasterize_pdf_page(output, 2)
    with pytest.raises(IndexError):
        rasterize_pdf_page(output, -1)


def test_pdf_page_count_unreadable_file(tmp_path):
    from models.errors import ExportError
    bogus = tmp_path / "faux.pdf"
    bogus.write_bytes(b"pas un pdf")
    with pytest.raises(ExportError):
        pdf_page_count(bogus)


# --------------------------------------------------------------------------- validation


def test_validate_reports_page_count_and_missing_content(tmp_path):
    pages = book(ReconstructionMode.EDITABLE, tmp_path, with_unanalysed=False)
    output = tmp_path / "livre.pdf"
    render_pdf(pages, output)
    too_many = validate_pdf(output, pages + [case_a(ReconstructionMode.EDITABLE, 9)])
    assert not too_many.ok and any(issue.code == "page_count_mismatch" for issue in too_many.issues)
    # page vide rendue alors que du contenu était attendu : page blanche, sans texte, image manquante
    blank_output = tmp_path / "blanc.pdf"
    render_pdf([unanalysed(ReconstructionMode.EDITABLE, 3)], blank_output)
    report = validate_pdf(blank_output, [pages[2]])
    codes = {issue.code for issue in report.issues if issue.severity == Severity.ERROR}
    assert codes == {"blank_page", "page_without_text", "image_missing"}
    unreadable = tmp_path / "casse.pdf"
    unreadable.write_bytes(b"%PDF-1.7 tronque")
    assert not validate_pdf(unreadable).ok


def test_validate_without_expectation(tmp_path):
    output = tmp_path / "simple.pdf"
    render_pdf([case_a(ReconstructionMode.EDITABLE)], output)
    report = validate_pdf(output)
    assert report.ok and report.expected_page_count is None and report.pages[0].text_similarity is None


def test_missing_image_is_an_error(tmp_path):
    images = make_images(tmp_path / "images")
    page = case_c(ReconstructionMode.FIDELITY, images)
    page.image_elements[0].file = str(tmp_path / "absente.png")
    report = render_pdf([page], tmp_path / "manque.pdf")
    assert not report.ok and any(issue.code == "image_missing" for issue in report.issues)
    assert not validate_pdf(tmp_path / "manque.pdf", [page]).ok


def test_font_fallback_is_reported(tmp_path):
    page = case_a(ReconstructionMode.EDITABLE)
    first = page.text_elements[1]
    first.style = first.style.model_copy(update={"font_family": "Police Absente"})
    report = render_pdf([page], tmp_path / "repli.pdf")
    assert any(issue.code == "font_fallback" and issue.block_id == first.block_id for issue in report.fallbacks)
    with pymupdf.open(tmp_path / "repli.pdf") as document:
        assert "Chapitre premier" in document[0].get_text()


# --------------------------------------------------------------------------- fonctions pures


def test_paragraph_html_escapes_and_breaks():
    assert paragraph_html("a < b & c\nligne 2") == "<p>a &lt; b &amp; c<br/>ligne 2</p>"


def test_style_css_uses_natural_line_height_and_no_spacing_in_box():
    style = STYLES[BlockKind.PARAGRAPH].model_copy(update={"space_before_pt": 8, "underline": True})
    css = style_css(style, "bookfont0", natural_ratio=1.15, font_scale=0.8)
    assert "font-size: 8.40pt" in css and "line-height: 1.3225" in css
    assert "margin-top: 0.00pt" in css and "text-decoration: underline" in css
    assert "text-align: justify" in css and 'font-family: "bookfont0"' in css
    assert "margin-top: 8.00pt" in style_css(style, "f", 1.15, include_spacing=True)


def test_text_similarity():
    assert pdf_service.text_similarity("Un  texte\nici", "Un texte ici") == 1.0
    assert pdf_service.text_similarity("abc", "xyz") < 0.5


# --------------------------------------------------------------------------- LibreOffice (exigence 20)


def test_conversion_unavailable_without_soffice(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SOFFICE_CMD", "soffice-introuvable-bookocr")
    result = convert_docx_to_pdf(tmp_path / "x.docx", tmp_path / "out")
    assert result.available is False and result.output is None
    assert "absent" in result.message


def test_conversion_command_with_fake_soffice(tmp_path, monkeypatch):
    """Vérifie la ligne de commande et la relecture du résultat sans LibreOffice (soffice simulé)."""
    log = tmp_path / "args.json"
    fake = tmp_path / "fake_soffice"
    fake.write_text(
        f"#!{sys.executable}\n"
        "import json, sys, pathlib, pymupdf\n"
        f"pathlib.Path({str(log)!r}).write_text(json.dumps(sys.argv[1:]))\n"
        "out = pathlib.Path(sys.argv[sys.argv.index('--outdir') + 1])\n"
        "source = pathlib.Path(sys.argv[-1])\n"
        "doc = pymupdf.open(); page = doc.new_page(); page.insert_text((72, 72), 'converti')\n"
        "doc.save(out / (source.stem + '.pdf'))\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(config, "SOFFICE_CMD", str(fake))
    source = tmp_path / "livre.docx"
    build_docx([case_a(ReconstructionMode.EDITABLE)], project(), source)
    result = convert_docx_to_pdf(source, tmp_path / "sortie", timeout=60)
    assert result.available and result.output == str(tmp_path / "sortie" / "livre.pdf")
    assert pdf_page_count(Path(result.output)) == 1
    arguments = json.loads(log.read_text())
    assert arguments[:2] == ["--headless", "--norestore"]
    assert arguments[2].startswith("-env:UserInstallation=file://")
    assert arguments[3:6] == ["--convert-to", "pdf", "--outdir"]


def test_conversion_failure_is_reported(tmp_path, monkeypatch):
    fake = tmp_path / "broken_soffice"
    fake.write_text("#!/bin/sh\necho 'erreur simulée' >&2\nexit 3\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(config, "SOFFICE_CMD", str(fake))
    source = tmp_path / "livre.docx"
    build_docx([case_a(ReconstructionMode.EDITABLE)], project(), source)
    result = convert_docx_to_pdf(source, tmp_path / "sortie")
    assert result.available and result.output is None and "erreur simulée" in result.message


@pytest.mark.libreoffice
@requires_soffice
def test_libreoffice_conversion_page_count(tmp_path):
    images = make_images(tmp_path / "images")
    for mode in ALL_MODES:
        pages = [case_a(mode), case_b(mode, images), case_c(mode, images), two_columns(mode)]
        source = tmp_path / f"livre_{mode.value}.docx"
        build_docx(pages, project(), source)
        result = convert_docx_to_pdf(source, tmp_path / "lo")
        assert result.available and result.output, result.message
        validation = validate_pdf(Path(result.output), None)
        assert validation.page_count == len(pages), f"{mode.value} : {validation.page_count} pages LibreOffice"
        assert not validation.issues, [issue.message for issue in validation.issues]


# --------------------------------------------------------------------------- script du skill


def test_check_pdf_script_accepts_export(tmp_path):
    pages = book(ReconstructionMode.HYBRID, tmp_path, with_unanalysed=False)
    output = tmp_path / "skill.pdf"
    render_pdf(pages, output)
    done = subprocess.run([sys.executable, str(CHECK_PDF), str(output), "--expect-pages", str(len(pages)),
                           "--expect-text", "Chapitre premier", "Les Couleurs du Sud", "Fig. 1"],
                          capture_output=True, text=True, timeout=120)
    result = json.loads(done.stdout)
    assert done.returncode == 0, result["failures"]
    assert result["non_embedded_fonts"] == [] and result["pages_without_text"] == []
    assert os.path.getsize(output) > 0
