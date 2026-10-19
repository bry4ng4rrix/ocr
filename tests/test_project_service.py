"""Projet persistant : création, ouverture, sauvegarde (nouveau processus), import, corbeille, ordre, historique.

Exigences couvertes (docs/TEST_STRATEGY.md) : 3 (originaux conservés), 10 (aller-retour), 11 (réglages),
12 (historique et restauration), 25 (import : formats, tailles, PDF et TIFF multi-pages).
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

import config
from models import (Alignment, BlockKind, Box, ImageBlock, ImageKind, PageStatus, Point, Project,
                    ReconstructionMode, STANDARD_FORMATS, TextBlock, TextStyle)
from models.errors import ImageReadError, PageNotFoundError, ProjectCorruptedError, ProjectNotFoundError
from services import image_service, project_service
from tests.conftest import ROOT, sha256

# --------------------------------------------------------------------------- données générées en mémoire


def _array(seed: int, height: int = 120, width: int = 80) -> np.ndarray:
    generator = np.random.default_rng(seed)
    array = np.full((height, width, 3), 235, np.uint8)
    array[10:40, 10:70] = generator.integers(0, 255, (30, 60, 3), dtype=np.uint8)
    return array


def _encode(array: np.ndarray, fmt: str) -> bytes:
    buffer = io.BytesIO()
    Image.fromarray(array).save(buffer, format=fmt)
    return buffer.getvalue()


def jpeg(seed: int = 1) -> bytes:
    return _encode(_array(seed), "JPEG")


def png(seed: int = 2) -> bytes:
    return _encode(_array(seed, 90, 140), "PNG")


def tiff(frames: int = 2, seed: int = 3) -> bytes:
    images = [Image.fromarray(_array(seed + index)) for index in range(frames)]
    buffer = io.BytesIO()
    images[0].save(buffer, format="TIFF", save_all=True, append_images=images[1:])
    return buffer.getvalue()


def pdf(pages: int = 3, tag: str = "livre") -> bytes:
    import pymupdf

    document = pymupdf.open()
    for index in range(pages):
        page = document.new_page(width=420, height=595)
        page.insert_text((50, 80), f"{tag} page {index + 1}", fontsize=18)
    data = document.tobytes()
    document.close()
    return data


def originals_digest(project: Project) -> dict[str, str]:
    folder = project_service.project_dir(project) / "originals"
    return {path.name: sha256(path) for path in sorted(folder.iterdir()) if path.is_file()}


def _fake_derived_files(project: Project, page) -> None:
    """Simule les fichiers produits par le traitement d'une page (image corrigée, illustration, fond)."""
    paths = project_service.page_paths(project, page)
    image_service.write_png(_array(50), paths.corrected)
    block = ImageBlock(kind=ImageKind.PHOTO, box=Box(x=0.1, y=0.1, w=0.5, h=0.3))
    block.file = f"images/{block.id}.png"
    image_service.write_png(_array(51), project_service.project_dir(project) / block.file)
    background = f"backgrounds/{page.id}_0.png"
    image_service.write_png(_array(52), project_service.project_dir(project) / background)
    page.image_blocks = [block]
    page.background_regions = [Box(x=0, y=0, w=1, h=0.5)]
    page.background_files = [background]
    page.text_blocks = [TextBlock(box=Box(x=0.1, y=0.5, w=0.8, h=0.2), ocr_text="Texte reconnu")]
    page.status = PageStatus.ANALYSED
    page.geometry.detected_quarter_turns = 1
    page.geometry.detection_done = True


@pytest.fixture
def project(data_dir) -> Project:
    return project_service.create_project("Mon livre", ["fra"])


REOPEN_SCRIPT = """
import sys
from services import project_service
sys.stdout.write(project_service.open_project(sys.argv[1]).model_dump_json())
"""


def reopen_in_new_process(project_id: str) -> Project:
    done = subprocess.run([sys.executable, "-c", REOPEN_SCRIPT, project_id], cwd=ROOT, env=dict(os.environ),
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    return Project.model_validate_json(done.stdout)


# --------------------------------------------------------------------------- création, ouverture, sauvegarde


def test_create_and_open_project(project, data_dir):
    root = project_service.project_dir(project)
    assert root == data_dir / "projects" / project.id
    for subdir in project_service.PROJECT_SUBDIRS:
        assert (root / subdir).is_dir(), subdir
    reopened = project_service.open_project(project.id)
    assert reopened == project
    assert reopened.languages == ["fra"]
    assert reopened.autosave_seconds == config.DEFAULT_AUTOSAVE_SECONDS


def test_create_project_requires_a_name(data_dir):
    with pytest.raises(ValueError, match="nom"):
        project_service.create_project("   ")


def test_list_projects_sorted_and_skips_unreadable(data_dir):
    first = project_service.create_project("Premier")
    second = project_service.create_project("Second")
    project_service.save_project(first)                      # devient le plus récent
    broken = data_dir / "projects" / "casse"
    broken.mkdir()
    (broken / "project.json").write_text("{pas du json", encoding="utf-8")

    summaries = project_service.list_projects()

    assert [summary.id for summary in summaries] == [first.id, second.id]
    assert summaries[0].name == "Premier" and summaries[0].page_count == 0


def test_list_projects_without_data_dir(data_dir):
    assert project_service.list_projects() == []


def test_open_missing_project(data_dir):
    with pytest.raises(ProjectNotFoundError):
        project_service.open_project("absent")
    with pytest.raises(ProjectNotFoundError):
        project_service.open_project("../evasion")


def test_save_is_atomic_and_keeps_previous_version(project):
    root = project_service.project_dir(project)
    first_update = project.updated_at
    project.name = "Nouveau titre"
    project_service.save_project(project)

    assert project.updated_at > first_update
    backup = json.loads((root / "project.json.bak").read_text(encoding="utf-8"))
    assert backup["name"] == "Mon livre"
    assert json.loads((root / "project.json").read_text(encoding="utf-8"))["name"] == "Nouveau titre"
    assert not list(root.glob(".*.tmp")), "fichier temporaire laissé derrière"


def test_invalid_values_are_not_saved(project):
    root = project_service.project_dir(project)
    before = (root / "project.json").read_text(encoding="utf-8")
    project.autosave_seconds = -5                             # affectation non validée par Pydantic
    with pytest.raises(ProjectCorruptedError):
        project_service.save_project(project)
    assert (root / "project.json").read_text(encoding="utf-8") == before


def test_backup_used_when_project_json_corrupted(project):
    root = project_service.project_dir(project)
    project.name = "Version sauvegardée"
    project_service.save_project(project)                     # .bak = « Mon livre »
    project.name = "Version courante"
    project_service.save_project(project)                     # .bak = « Version sauvegardée »
    (root / "project.json").write_text('{"tronqué": ', encoding="utf-8")

    reopened = project_service.open_project(project.id)

    assert reopened.name == "Version sauvegardée"
    assert (root / "project.json.corrupted").exists()
    # la sauvegarde suivante répare project.json sans écraser le .bak sain par le fichier corrompu
    project_service.save_project(reopened)
    assert json.loads((root / "project.json.bak").read_text(encoding="utf-8"))["name"] == "Version sauvegardée"
    assert project_service.open_project(project.id).name == "Version sauvegardée"


def test_corrupted_without_usable_backup(project):
    root = project_service.project_dir(project)
    (root / "project.json").write_text("[]", encoding="utf-8")
    with pytest.raises(ProjectCorruptedError):
        project_service.open_project(project.id)
    (root / "project.json.bak").write_text("{}", encoding="utf-8")   # JSON valide, modèle invalide
    with pytest.raises(ProjectCorruptedError, match="sauvegarde précédente"):
        project_service.open_project(project.id)


def test_future_schema_version_is_refused(project):
    root = project_service.project_dir(project)
    project_service.save_project(project)                       # un .bak valide existe
    data = json.loads((root / "project.json").read_text(encoding="utf-8"))
    data["schema_version"] = 99
    (root / "project.json").write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ProjectCorruptedError, match="version plus récente"):
        project_service.open_project(project.id)


def test_should_autosave(project):
    project.autosave_seconds = 30
    assert not project_service.should_autosave(project, 100.0, 120.0)
    assert project_service.should_autosave(project, 100.0, 130.0)
    project.autosave_seconds = 0
    assert not project_service.should_autosave(project, 0.0, 10_000.0)


# --------------------------------------------------------------------------- aller-retour dans un nouveau processus


def test_roundtrip_preserves_everything(project):
    report = project_service.import_files(project, [("a.jpg", jpeg(1)), ("b.png", png(2)), ("c.pdf", pdf(2))])
    assert len(report.added_page_ids) == 4
    first, second, third, fourth = report.added_page_ids
    project_service.reorder_pages(project, [third, first, fourth, second])

    page = project.page(first).model_copy(deep=True)
    page.text_blocks = [
        TextBlock(kind=BlockKind.HEADING_1, box=Box(x=0.1, y=0.05, w=0.8, h=0.08), ocr_text="Titre lu",
                  corrected_text="Titre corrigé « é è ç œ »", style_override=TextStyle(italic=True), order=0),
        TextBlock(box=Box(x=0.1, y=0.2, w=0.8, h=0.3), ocr_text="Paragraphe", order=1),
    ]
    page.image_blocks = [ImageBlock(box=Box(x=0.2, y=0.55, w=0.6, h=0.3), file="images/x.png", order=2)]
    page.geometry.manual_quarter_turns = 3
    page.geometry.manual_quad = [Point(x=0.1, y=0.1), Point(x=0.9, y=0.1), Point(x=0.9, y=0.9), Point(x=0.1, y=0.9)]
    page.geometry.manual_skew_deg = -1.5
    page.enhancement.contrast = 25
    page.enhancement.white_balance = "auto"
    page.style_rules = {BlockKind.PARAGRAPH: TextStyle(font_size_pt=11.5, alignment=Alignment.JUSTIFY)}
    page.format_override = STANDARD_FORMATS["A5"]
    project_service.update_page(project, page, reason="Édition")

    project.typography.document_font = "DejaVu Serif"
    project.typography.kind_styles = {BlockKind.HEADING_1: TextStyle(bold=True, font_size_pt=22, color="#112233")}
    project.export.mode = ReconstructionMode.HYBRID
    project.export.page_format = "custom"
    project.export.custom_width_mm = 160
    project.export.margins_override_mm = (10.0, 12.0, 14.0, 16.0)
    project_service.save_project(project)

    reloaded = reopen_in_new_process(project.id)

    assert reloaded == project                                  # égalité complète du modèle
    assert [p.id for p in reloaded.pages] == [third, first, fourth, second]
    restored = reloaded.page(first)
    assert restored.text_blocks[0].text == "Titre corrigé « é è ç œ »"
    assert restored.text_blocks[0].style_override.italic is True
    assert restored.image_blocks[0].box == Box(x=0.2, y=0.55, w=0.6, h=0.3)
    assert restored.geometry.manual_quarter_turns == 3 and restored.geometry.manual_skew_deg == -1.5
    assert restored.enhancement.contrast == 25
    assert restored.style_rules[BlockKind.PARAGRAPH].font_size_pt == 11.5
    assert restored.format_override == STANDARD_FORMATS["A5"]


def test_settings_survive_reopen(project):
    project.languages = ["deu", "fra"]
    project.ocr_psm = 6
    project.background_method = "fill"
    project.autosave_seconds = 120
    project.typography.default_line_spacing = 1.4
    project.typography.default_alignment = Alignment.JUSTIFY
    project.export.mode = ReconstructionMode.FIDELITY
    project.export.page_format = "A4"
    project.export.overflow_policy = "report"
    project.export.include_page_numbers = False
    project_service.save_project(project)

    reloaded = reopen_in_new_process(project.id)

    assert reloaded.languages == ["deu", "fra"] and reloaded.ocr_psm == 6
    assert reloaded.background_method == "fill" and reloaded.autosave_seconds == 120
    assert reloaded.typography == project.typography
    assert reloaded.export == project.export


# --------------------------------------------------------------------------- import


def test_import_formats(project):
    files = [("photo.jpg", jpeg(1)), ("scan.png", png(2)), ("pages.tiff", tiff(2)), ("livre.pdf", pdf(3))]
    report = project_service.import_files(project, files)

    assert report.issues == []
    assert len(report.added_page_ids) == 7
    assert [page.id for page in project.pages] == report.added_page_ids
    assert [(page.original_name, page.source_index) for page in project.pages] == [
        ("photo.jpg", 0), ("scan.png", 0), ("pages.tiff", 0), ("pages.tiff", 1),
        ("livre.pdf", 0), ("livre.pdf", 1), ("livre.pdf", 2)]
    root = project_service.project_dir(project)
    # un seul fichier stocké par source, octet pour octet, nommé par son empreinte
    assert len(list((root / "originals").iterdir())) == 4
    for name, data in files:
        stored = {page.source_file for page in project.pages if page.original_name == name}
        assert len(stored) == 1
        source = stored.pop()
        assert (root / source).read_bytes() == data
        assert Path(source).name.startswith(image_service.sha256_bytes(data)[:16])
    jpg_page, png_page, tiff_page = project.pages[0], project.pages[1], project.pages[3]
    assert (jpg_page.source_width, jpg_page.source_height) == (80, 120)
    assert (png_page.source_width, png_page.source_height) == (140, 90)
    assert project_service.load_original(project, tiff_page).shape == (120, 80, 3)
    pdf_page = project.pages[4]
    assert pdf_page.source_width == round(420 * config.PDF_IMPORT_DPI / 72)
    for page in project.pages:
        thumbnail = project_service.load_thumbnail(project, page)
        assert thumbnail is not None and max(thumbnail.shape[:2]) <= config.THUMBNAIL_MAX_SIDE
        assert page.status == PageStatus.IMPORTED
    assert project_service.open_project(project.id) == project


def test_import_at_position(project):
    project_service.import_files(project, [("a.jpg", jpeg(1)), ("b.jpg", jpeg(2))])
    first, last = (page.id for page in project.pages)
    report = project_service.import_files(project, [("c.jpg", jpeg(3))], position=1)
    assert [page.id for page in project.pages] == [first, report.added_page_ids[0], last]
    assert [page.id for page in project_service.open_project(project.id).pages] == [page.id for page in project.pages]


def test_duplicate_is_ignored(project):
    data = jpeg(7)
    project_service.import_files(project, [("a.jpg", data)])
    book = pdf(2)
    report = project_service.import_files(project, [("copie.jpg", data), ("b.pdf", book), ("b-bis.pdf", book)])

    assert len(report.added_page_ids) == 2                        # seules les 2 pages du premier PDF
    codes = [issue.code for issue in report.issues]
    assert codes == ["duplicate_page"] * 3
    assert len(project.pages) == 3


def test_invalid_file_reported_without_blocking_others(project):
    broken_jpeg = jpeg(4)[:200]
    files = [("bon1.jpg", jpeg(1)), ("notes.txt", b"bonjour"), ("abime.jpg", broken_jpeg), ("vide.png", b""),
             ("faux.pdf", b"%PDF-pas vraiment"), ("bon2.png", png(2))]
    report = project_service.import_files(project, files)

    assert [page.original_name for page in project.pages] == ["bon1.jpg", "bon2.png"]
    assert len(report.issues) == 4
    assert {issue.code for issue in report.issues} <= {"import_unsupported", "import_unreadable"}
    for name in ("notes.txt", "abime.jpg", "vide.png", "faux.pdf"):
        assert sum(name in issue.message for issue in report.issues) == 1, name
    root = project_service.project_dir(project)
    assert len(list((root / "originals").iterdir())) == 2          # rien stocké pour les fichiers refusés


def test_rejects_oversize(project, monkeypatch):
    monkeypatch.setattr(config, "MAX_FILE_MB", 0)                 # tout fichier non vide dépasse 0 Mo
    report = project_service.import_files(project, [("a.jpg", jpeg(1))])
    assert report.added_page_ids == [] and report.issues[0].code == "import_too_large"
    monkeypatch.setattr(config, "MAX_FILE_MB", 80)

    monkeypatch.setattr(config, "MAX_PAGES_PER_PROJECT", 3)
    report = project_service.import_files(project, [("gros.pdf", pdf(5)), ("encore.jpg", jpeg(2))])
    assert len(report.added_page_ids) == 3
    assert [issue.code for issue in report.issues] == ["page_limit_reached", "page_limit_reached"]
    assert [page.source_index for page in project.pages] == [0, 1, 2]
    assert len(project_service.open_project(project.id).pages) == 3


# --------------------------------------------------------------------------- originaux, remplacement, suppression, ordre


def test_originals_unchanged_through_project_operations(project):
    files = [("a.jpg", jpeg(1)), ("b.tiff", tiff(2)), ("c.pdf", pdf(3))]
    expected = {image_service.sha256_bytes(data) for _, data in files}
    project_service.import_files(project, files)
    digests = originals_digest(project)
    assert set(digests.values()) == expected

    ids = [page.id for page in project.pages]
    project_service.reorder_pages(project, list(reversed(ids)))
    assert originals_digest(project) == digests

    project_service.replace_page_source(project, ids[0], "nouvelle.png", png(9))
    after_replace = originals_digest(project)
    assert {name: digest for name, digest in after_replace.items() if name in digests} == digests

    project_service.delete_pages(project, [ids[3]])                # une page du PDF : original partagé conservé
    assert originals_digest(project) == after_replace
    project_service.set_manual_rotation(project, ids[1], 2)
    assert originals_digest(project) == after_replace
    trash_copies = list((project_service.project_dir(project) / "trash").rglob("originals/*"))
    assert all(sha256(path) in expected | {image_service.sha256_bytes(png(9))} for path in trash_copies)


def test_replace_page_source_resets_only_that_page(project):
    project_service.import_files(project, [("a.jpg", jpeg(1)), ("b.jpg", jpeg(2)), ("c.jpg", jpeg(3))])
    target_id = project.pages[1].id
    for page in project.pages:
        _fake_derived_files(project, page)
    target = project.page(target_id)
    target.enhancement.contrast = 30
    target.geometry.manual_quarter_turns = 1
    target.style_rules = {BlockKind.PARAGRAPH: TextStyle(bold=True)}
    project_service.save_project(project)
    others_before = [page.model_copy(deep=True) for page in project.pages if page.id != target_id]
    old_paths = project_service.page_paths(project, target)
    old_files = [old_paths.corrected, old_paths.thumbnail, project_service.project_dir(project) /
                 target.image_blocks[0].file, project_service.project_dir(project) / target.background_files[0]]
    old_original = old_paths.original
    old_digest = sha256(old_original)

    new_page = project_service.replace_page_source(project, target_id, "refaite.png", png(5))

    assert new_page.id == target_id and project.pages[1] is new_page
    assert new_page.original_name == "refaite.png" and new_page.source_sha256 == image_service.sha256_bytes(png(5))
    assert new_page.status == PageStatus.IMPORTED
    assert new_page.text_blocks == [] and new_page.image_blocks == [] and new_page.background_files == []
    assert not new_page.geometry.detection_done and new_page.geometry.detected_quarter_turns is None
    assert new_page.geometry.manual_quarter_turns == 0
    assert new_page.enhancement.contrast == 30 and new_page.style_rules[BlockKind.PARAGRAPH].bold
    assert [page for page in project.pages if page.id != target_id] == others_before
    assert not any(path.exists() for path in old_files[:1] + old_files[2:])
    trash = project_service.project_dir(project) / "trash" / target_id
    assert len(list(trash.rglob("corrected/*.png"))) == 1 and len(list(trash.rglob("images/*.png"))) == 1
    assert old_original.exists() and sha256(old_original) == old_digest     # l'historique peut y revenir
    assert project_service.load_thumbnail(project, new_page).shape[:2] == (90, 140)
    reloaded = project_service.open_project(project.id)
    assert [page.id for page in reloaded.pages] == [page.id for page in project.pages]
    assert reloaded.page(target_id).original_name == "refaite.png"
    assert project_service.list_page_versions(project, target_id)[0].reason == "Avant remplacement de la photo"


def test_replace_with_invalid_file_changes_nothing(project):
    project_service.import_files(project, [("a.jpg", jpeg(1))])
    before = project.model_copy(deep=True)
    with pytest.raises(ImageReadError):
        project_service.replace_page_source(project, project.pages[0].id, "x.jpg", b"pas une image")
    assert project.pages == before.pages
    assert project_service.list_page_versions(project, project.pages[0].id) == []


def test_delete_pages_moves_to_trash_and_keeps_shared_original(project):
    project_service.import_files(project, [("livre.pdf", pdf(2)), ("seule.jpg", jpeg(1))])
    pdf_first, pdf_second, single = project.pages
    for page in project.pages:
        _fake_derived_files(project, page)
    project_service.save_project(project)
    project_service.snapshot_page(project, pdf_first.id, "test")
    root = project_service.project_dir(project)
    shared_original = root / pdf_first.source_file

    project_service.delete_pages(project, [pdf_first.id, single.id])

    assert [page.id for page in project.pages] == [pdf_second.id]
    assert shared_original.exists()                               # encore utilisé par la 2e page du PDF
    assert not (root / single.source_file).exists()
    assert (root / "trash" / single.id / single.source_file).exists()
    snapshot = json.loads((root / "trash" / pdf_first.id / "page.json").read_text(encoding="utf-8"))
    assert snapshot["page"]["id"] == pdf_first.id and snapshot["position"] == 0
    assert (root / "trash" / pdf_first.id / "corrected" / f"{pdf_first.id}.png").exists()
    assert (root / "trash" / pdf_first.id / "history" / pdf_first.id).is_dir()
    assert not (root / "corrected" / f"{pdf_first.id}.png").exists()
    assert (root / "corrected" / f"{pdf_second.id}.png").exists()
    assert [page.id for page in project_service.open_project(project.id).pages] == [pdf_second.id]

    project_service.delete_pages(project, [pdf_second.id])
    assert not shared_original.exists()
    assert (root / "trash" / pdf_second.id / pdf_second.source_file).exists()


def test_delete_unknown_page_changes_nothing(project):
    project_service.import_files(project, [("a.jpg", jpeg(1))])
    with pytest.raises(PageNotFoundError):
        project_service.delete_pages(project, [project.pages[0].id, "inconnue"])
    assert len(project.pages) == 1
    assert not any((project_service.project_dir(project) / "trash").iterdir())


def test_reorder_pages(project):
    project_service.import_files(project, [(f"{n}.jpg", jpeg(n)) for n in range(4)])
    ids = [page.id for page in project.pages]
    wanted = [ids[2], ids[0], ids[3], ids[1]]
    project_service.reorder_pages(project, wanted)
    assert [page.id for page in project_service.open_project(project.id).pages] == wanted

    for invalid in ([ids[0], ids[1], ids[2]], wanted + [ids[0]], [ids[0], ids[0], ids[1], ids[2]],
                    [ids[0], ids[1], ids[2], "autre"]):
        with pytest.raises(ValueError, match="exactement une fois"):
            project_service.reorder_pages(project, invalid)
    assert [page.id for page in project.pages] == wanted


def test_manual_rotation(project):
    project_service.import_files(project, [("a.jpg", jpeg(1))])
    page_id = project.pages[0].id
    page = project_service.set_manual_rotation(project, page_id, 3)
    assert page.geometry.manual_quarter_turns == 3 and project.page(page_id) is page
    assert project_service.open_project(project.id).page(page_id).geometry.manual_quarter_turns == 3
    assert [version.reason for version in project_service.list_page_versions(project, page_id)] == ["Rotation manuelle"]
    for invalid in (4, -1, 1.5, True):
        with pytest.raises(ValueError):
            project_service.set_manual_rotation(project, page_id, invalid)


# --------------------------------------------------------------------------- historique


def test_restore_previous_version(project):
    project_service.import_files(project, [("a.jpg", jpeg(1)), ("b.jpg", jpeg(2))])
    page_id = project.pages[1].id
    page = project.page(page_id).model_copy(deep=True)
    page.text_blocks = [TextBlock(box=Box(x=0.1, y=0.1, w=0.5, h=0.1), ocr_text="lu", corrected_text="Version 1")]
    project_service.update_page(project, page, reason="Correction 1")
    edited = project.page(page_id).model_copy(deep=True)
    edited.text_blocks[0].corrected_text = "Version 2"
    project_service.update_page(project, edited, reason="Correction 2")

    versions = project_service.list_page_versions(project, page_id)
    assert [version.reason for version in versions] == ["Correction 2", "Correction 1"]

    restored = project_service.restore_page_version(project, page_id, versions[0].version_id)

    assert restored.text_blocks[0].corrected_text == "Version 1"
    assert project.pages[1] is restored
    reloaded = reopen_in_new_process(project.id)
    assert reloaded.page(page_id).text_blocks[0].corrected_text == "Version 1"
    assert [p.id for p in reloaded.pages] == [p.id for p in project.pages]
    reasons = [version.reason for version in project_service.list_page_versions(project, page_id)]
    assert reasons[0] == "Avant restauration"
    # la version « avant restauration » permet de revenir à « Version 2 »
    latest = project_service.list_page_versions(project, page_id)[0].version_id
    assert project_service.restore_page_version(project, page_id, latest).text_blocks[0].corrected_text == "Version 2"


def test_restore_unknown_version(project):
    project_service.import_files(project, [("a.jpg", jpeg(1))])
    page_id = project.pages[0].id
    for version_id in ("20260101T000000000000Z_deadbeef", "../../project"):
        with pytest.raises(PageNotFoundError):
            project_service.restore_page_version(project, page_id, version_id)


def test_update_page_in_place_snapshots_saved_state(project):
    project_service.import_files(project, [("a.jpg", jpeg(1))])
    page = project.pages[0]
    page.enhancement.brightness = 40                              # modification sur place, non enregistrée
    project_service.update_page(project, page, reason="Luminosité")
    version = project_service.list_page_versions(project, page.id)[0]
    restored = project_service.restore_page_version(project, page.id, version.version_id)
    assert restored.enhancement.brightness == 0


def test_history_depth_is_bounded(project, monkeypatch):
    monkeypatch.setattr(config, "HISTORY_DEPTH", 3)
    project_service.import_files(project, [("a.jpg", jpeg(1))])
    page_id = project.pages[0].id
    created = [project_service.snapshot_page(project, page_id, f"étape {n}") for n in range(5)]
    versions = project_service.list_page_versions(project, page_id)
    assert [version.version_id for version in versions] == list(reversed(created[-3:]))
    assert [version.reason for version in versions] == ["étape 4", "étape 3", "étape 2"]


def test_restore_after_replacement_recovers_source_and_files(project):
    project_service.import_files(project, [("a.jpg", jpeg(1)), ("b.jpg", jpeg(2))])
    page = project.pages[0]
    _fake_derived_files(project, page)
    project_service.save_project(project)
    original_page = page.model_copy(deep=True)
    root = project_service.project_dir(project)

    project_service.replace_page_source(project, page.id, "c.jpg", jpeg(3))
    project_service.delete_pages(project, [])                     # sans effet
    before_replace = project_service.list_page_versions(project, page.id)[0]
    restored = project_service.restore_page_version(project, page.id, before_replace.version_id)

    assert restored.source_file == original_page.source_file and restored.status == PageStatus.ANALYSED
    assert restored.text_blocks == original_page.text_blocks
    assert (root / restored.image_blocks[0].file).exists()
    assert (root / restored.background_files[0]).exists()
    assert project_service.page_paths(project, restored).corrected.exists()
    assert not any(issue.code == "files_missing" for issue in restored.issues)
    assert project_service.load_thumbnail(project, restored).shape[:2] == (120, 80)


def test_restore_recovers_original_from_trash(project):
    project_service.import_files(project, [("livre.pdf", pdf(2))])
    first, second = project.pages
    version = project_service.snapshot_page(project, second.id, "test")
    # la 2e page reçoit une autre photo, puis la 1re est supprimée : l'original du PDF part à la corbeille
    project_service.replace_page_source(project, second.id, "c.jpg", jpeg(3))
    project_service.delete_pages(project, [first.id])
    root = project_service.project_dir(project)
    assert not (root / first.source_file).exists()

    restored = project_service.restore_page_version(project, second.id, version)

    assert restored.source_file == first.source_file and restored.source_index == 1
    assert (root / restored.source_file).exists()
    assert sha256(root / restored.source_file) == restored.source_sha256
    assert project_service.load_original(project, restored).ndim == 3
    assert not any(issue.code == "files_missing" for issue in restored.issues)


def test_load_thumbnail_regenerated_when_missing(project):
    project_service.import_files(project, [("a.jpg", jpeg(1))])
    page = project.pages[0]
    project_service.page_paths(project, page).thumbnail.unlink()
    assert project_service.load_thumbnail(project, page).shape[:2] == (120, 80)
    assert project_service.page_paths(project, page).thumbnail.exists()
