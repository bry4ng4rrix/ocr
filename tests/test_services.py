import io
import zipfile

import numpy as np
import pytest
from PIL import Image

from core.document_models import Box, GeometrySettings, ImageBlock, OCRLine, OCRWord, Page, TextBlock
from core.errors import ProjectError
from core.illustration_extraction import DetectedRegion
from services import processing_service
from services import project_service as ps
from services import storage_service as ss
from services.history import History
from tests.synthetic import illustrated_page, simple_page


def png_bytes(rgb, fmt="PNG", **kw) -> bytes:
    buffer = io.BytesIO()
    Image.fromarray(rgb).save(buffer, fmt, **kw)
    return buffer.getvalue()


def tiff_two_frames() -> bytes:
    a, b = Image.fromarray(simple_page().image), Image.fromarray(illustrated_page().image)
    buffer = io.BytesIO()
    a.save(buffer, "TIFF", save_all=True, append_images=[b])
    return buffer.getvalue()


def pdf_two_pages() -> bytes:
    buffer = io.BytesIO()
    pages = [Image.fromarray(simple_page().image), Image.fromarray(illustrated_page().image)]
    pages[0].save(buffer, "PDF", save_all=True, append_images=pages[1:], resolution=150)
    return buffer.getvalue()


def test_page_sized_illustration_does_not_mask_or_replace_confirmed_page_text():
    page_region = DetectedRegion(Box(), "drawing", 60.0)
    illustration = DetectedRegion(Box(x=0.2, y=0.2, width=0.3, height=0.3), "photo", 90.0)
    words = [(50 + (i % 5) * 180, 50 + (i // 5) * 120,
              120 + (i % 5) * 180, 75 + (i // 5) * 120) for i in range(20)]

    assert processing_service._ocr_mask_regions([page_region, illustration]) == [illustration.box]
    assert processing_service._filter_page_backgrounds([page_region, illustration], words, (1000, 1000, 3)) \
        == [illustration]
    assert processing_service._filter_page_backgrounds([page_region], words[:19], (1000, 1000, 3)) == [page_region]


def test_merge_text_blocks_keeps_text_correction_and_combines_ocr():
    page = Page(original_path="originals/test.png")
    first = TextBlock(x=0.1, y=0.1, width=0.4, height=0.1, text="Correction",
                      ocr_text="Correcion", reading_order=0)
    second = TextBlock(x=0.1, y=0.25, width=0.5, height=0.1, text="Deuxième",
                       ocr_text="Deuxieme", reading_order=1)
    page.blocks = [first, second]

    merged = ps.merge_text_blocks(page, first.id)

    assert merged.text == "Correction\n\nDeuxième"
    assert merged.ocr_text == "Correcion\n\nDeuxieme"
    assert (merged.x, merged.y, merged.width, merged.height) == pytest.approx((0.1, 0.1, 0.5, 0.25))
    assert page.blocks == [merged]


def test_split_text_block_preserves_text_and_reading_order():
    page = Page(original_path="originals/test.png")
    first = TextBlock(x=0.1, y=0.1, width=0.6, height=0.6,
                      text="ligne 1\nligne 2\nligne 3", ocr_text="ocr 1\nocr 2\nocr 3",
                      reading_order=0)
    following = TextBlock(x=0.1, y=0.8, width=0.6, height=0.1, text="suivant", reading_order=1)
    page.blocks = [first, following]

    second = ps.split_text_block(page, first, 2)

    assert first.text == "ligne 1\nligne 2"
    assert first.ocr_text == "ocr 1\nocr 2"
    assert second.text == "ligne 3"
    assert second.ocr_text == "ocr 3"
    assert first.y + first.height == pytest.approx(second.y)
    assert [block.reading_order for block in page.ordered_blocks()] == [0, 1, 2]


def test_import_several_formats_keeps_originals_and_skips_duplicates(workspace):
    project = ps.create_project("Livre", ["fra"])
    jpg = png_bytes(simple_page().image, "JPEG", quality=90)
    files = [("p1.jpg", jpg), ("p2.png", png_bytes(illustrated_page().image)), ("scan.tiff", tiff_two_frames()),
             ("doc.pdf", pdf_two_pages()), ("p1-copie.jpg", jpg), ("faux.png", b"pas une image"),
             ("note.txt", b"bonjour")]
    report = ps.import_files(project, files)
    assert len(report.added) == 6           # 1 + 1 + 2 TIFF frames + 2 PDF pages
    assert len(report.skipped) == 1
    assert len(report.errors) == 2 and any("faux.png" in e for e in report.errors)
    storage = ss.ProjectStorage(project.id)
    assert storage.read_bytes(project.pages[0].original_path) == jpg     # original untouched
    tiff_pages = [p for p in project.pages if p.original_filename == "scan.tiff"]
    assert [p.source_index for p in tiff_pages] == [0, 1]
    assert all(storage.exists(p.thumbnail_path) for p in project.pages)
    reloaded = ss.load_project(project.id)
    assert [p.id for p in reloaded.pages] == [p.id for p in project.pages]


def test_reorder_move_delete(workspace):
    project = ps.create_project("Ordre")
    ps.import_files(project, [(f"{i}.png", png_bytes(np.full((80, 60, 3), 40 * i, np.uint8))) for i in range(1, 4)])
    ids = [p.id for p in project.pages]
    ps.move_page(project, ids[2], -2)
    assert [p.id for p in project.pages] == [ids[2], ids[0], ids[1]]
    ps.reorder_pages(project, ids)
    assert [p.id for p in project.pages] == ids
    with pytest.raises(ProjectError):
        ps.reorder_pages(project, ids[:2])
    original = project.pages[1].original_path
    ps.delete_page(project, ids[1])
    assert [p.id for p in project.pages] == [ids[0], ids[2]]
    assert not ss.ProjectStorage(project.id).exists(original)


def test_paths_cannot_escape_the_project(workspace):
    project = ps.create_project("Sûr")
    storage = ss.ProjectStorage(project.id)
    for bad in ("../x", "/etc/passwd", "originals/../../x", "a\\b"):
        with pytest.raises(ProjectError):
            storage.path(bad)
    raw = project.model_dump(mode="json")
    raw["pages"] = [{"original_path": "../../etc/passwd"}]
    with pytest.raises(ProjectError):
        ss.parse_project(__import__("json").dumps(raw))
    with pytest.raises(ProjectError):
        ss.ProjectStorage("../evil")


def test_zip_backup_restore_and_zip_slip(workspace):
    project = ps.create_project("Sauvegarde")
    ps.import_files(project, [("a.png", png_bytes(simple_page().image))])
    project.pages[0].blocks.append(TextBlock(x=0.1, y=0.1, width=0.5, height=0.1, text="corrigé", ocr_text="corige"))
    data = ss.export_zip(project)
    restored = ss.import_zip(data)
    assert restored.id != project.id         # id already present -> new project
    assert restored.pages[0].blocks[0].text == "corrigé"
    assert ss.ProjectStorage(restored.id).exists(restored.pages[0].original_path)
    evil = io.BytesIO()
    with zipfile.ZipFile(evil, "w") as archive:
        archive.writestr("project.json", project.to_json())
        archive.writestr("originals/../../../evil.txt", "x")
    with pytest.raises(ProjectError):
        ss.import_zip(evil.getvalue())
    assert not (workspace / "evil.txt").exists()


def test_corrupted_project_json_falls_back_to_backup(workspace):
    project = ps.create_project("Secours")
    ss.save_project(project)          # creates a .bak
    path = ss.ProjectStorage(project.id).root / "project.json"
    path.write_text("{ cassé", encoding="utf-8")
    assert ss.load_project(project.id).name == "Secours"


def test_rotation_turns_blocks_lines_quads_and_illustrations(workspace):
    project = ps.create_project("Rotation")
    ps.import_files(project, [("a.png", png_bytes(simple_page().image))])
    page = project.pages[0]
    page.frame_width, page.frame_height = 1000, 1500
    page.geometry = GeometrySettings(detected_quad=[(0.1, 0.1), (0.9, 0.1), (0.9, 0.9), (0.1, 0.9)], trim_left=5)
    page.ocr_geometry_signature = page.geometry.signature()
    word = OCRWord(text="mot", x=0.1, y=0.2, width=0.1, height=0.02, confidence=90)
    page.blocks.append(TextBlock(x=0.1, y=0.2, width=0.3, height=0.05, text="mot",
                                 lines=[OCRLine(x=0.1, y=0.2, width=0.1, height=0.02, words=[word])]))
    src = np.zeros((20, 40, 3), np.uint8)
    src[:, :20] = (255, 0, 0)
    storage = ss.ProjectStorage(project.id)
    storage.write_bytes("illustrations/i.png", png_bytes(src))
    page.blocks.append(ImageBlock(x=0.5, y=0.5, width=0.4, height=0.2, image_path="illustrations/i.png",
                                  original_image_path="illustrations/i.png", source_width=40, source_height=20))
    ps.rotate_page(project, page, 90)
    text, image = page.blocks
    assert (round(text.x, 4), round(text.y, 4), round(text.width, 4), round(text.height, 4)) == (0.75, 0.1, 0.05, 0.3)
    assert text.lines[0].words[0].x == pytest.approx(0.78)
    assert page.geometry.trim_top == 5 and page.geometry.trim_left == 0
    assert page.geometry.detected_quad[0] == pytest.approx((0.1, 0.1))
    assert (page.frame_width, page.frame_height) == (1500, 1000)
    assert not page.ocr_outdated                 # coordinates were turned with the page
    assert (image.source_width, image.source_height) == (20, 40)
    with Image.open(storage.path(image.image_path)) as im:
        top = np.array(im)[0]
    assert tuple(top[-1]) == (255, 0, 0)         # left half (red) is now on top after a clockwise turn
    assert storage.exists("illustrations/i.png")  # first extraction kept


def test_history_undo_redo(workspace):
    project = ps.create_project("Historique")
    ps.import_files(project, [("a.png", png_bytes(simple_page().image))])
    page = project.pages[0]
    history = History(project.id)
    history.record(page, "ajout")
    block = ps.add_text_block(page)
    history.record(page, "texte")
    block.text = "modifié"
    assert history.undo(project) == "texte"
    assert project.pages[0].blocks[0].text == "Nouveau texte"
    assert history.undo(project) == "ajout"
    assert project.pages[0].blocks == []
    assert history.redo(project) == "ajout"
    assert history.redo(project) == "texte"
    assert project.pages[0].blocks[0].text == "modifié"


def test_block_edits_are_clamped_and_reading_order_moves(workspace):
    project = ps.create_project("Blocs")
    ps.import_files(project, [("a.png", png_bytes(simple_page().image))])
    page = project.pages[0]
    a = ps.add_text_block(page, text="A")
    b = ps.add_text_block(page, text="B")
    warnings = ps.set_block_box(a, 0.9, 0.95, 0.3, 0.1)
    assert warnings and a.x + a.width <= 1 and a.y + a.height <= 1
    ps.move_in_reading_order(page, b.id, -1)
    assert [blk.text for blk in page.ordered_blocks()] == ["B", "A"]
    img = ps.add_image_block(project, page, png_bytes(illustrated_page().image), "x.png")
    ps.delete_block(project, page, a.id)
    assert page.block(a.id) is None and page.block(img.id) is not None
    with pytest.raises(Exception):
        ps.add_image_block(project, page, b"%PDF-1.4 nope", "x.pdf")
