"""Check a generated PDF by reading it back with pypdfium2.

For each expected page: page size against the sheet geometry, extractable text compared with the
text of the displayed blocks (similarity 0..1), image objects found against images expected. The
figures measure the PDF against the project; they say nothing about how close the blocks are to
the scan. The result is a JSON-serialisable dict.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import BinaryIO

from .document_models import ImageBlock, Page, Project, TextBlock
from .pdf_generator import _displayed, _sheet

SIZE_TOLERANCE_PT = 1.0
TEXT_OK = 0.9
MAX_EXACT_CHARS = 20000     # beyond this, a cheaper similarity bound is used


def normalize_text(text: str) -> str:
    """Text without spaces, hyphens and case: line breaks and hyphenation do not count."""
    return re.sub(r"[\s\-‐‑­]+", "", text).lower()


def similarity(expected: str, found: str) -> float:
    a, b = normalize_text(expected), normalize_text(found)
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    matcher = SequenceMatcher(None, a, b, autojunk=False)
    return matcher.ratio() if len(a) + len(b) <= 2 * MAX_EXACT_CHARS else matcher.quick_ratio()


def _expected_text(page: Page, project: Project, mode: str) -> str | None:
    render = project.render
    if mode == "facsimile":
        if not render.facsimile_text_layer:
            return None
        blocks = [b for b in page.ordered_blocks() if isinstance(b, TextBlock) and b.text.strip()
                  and (render.include_hidden_blocks or not b.hidden)]
    else:
        blocks = [b for b in _displayed(page, render) if isinstance(b, TextBlock)]
    return "\n".join(b.text for b in blocks)


def _expected_images(page: Page, project: Project, mode: str) -> int:
    if mode == "facsimile":
        return 1
    return sum(1 for b in _displayed(page, project.render) if isinstance(b, ImageBlock))


def _count_images(pdf_page) -> int:
    import pypdfium2.raw as pdfium_c
    return sum(1 for _ in pdf_page.get_objects(filter=[pdfium_c.FPDF_PAGEOBJ_IMAGE], max_depth=15))


def validate_pdf(path: Path | str | bytes | BinaryIO, project: Project, mode: str = "reconstruction",
                 page_ids: list[str] | None = None) -> dict:
    """Read the PDF back and compare it with the project pages it should contain (see module doc)."""
    import pypdfium2 as pdfium

    pages = [p for p in project.pages if page_ids is None or p.id in set(page_ids)]
    report: dict = {"mode": mode, "expected_pages": len(pages), "page_count": 0, "page_count_ok": False,
                    "pages": [], "summary": {}, "errors": [], "warnings": [], "ok": False}
    try:
        source = path if isinstance(path, (bytes, bytearray)) or hasattr(path, "read") else str(path)
        pdf = pdfium.PdfDocument(source)
    except Exception as exc:
        report["errors"].append(f"PDF illisible : {exc}")
        return report
    try:
        count = len(pdf)
        report["page_count"] = count
        report["page_count_ok"] = count == len(pages)
        if count < len(pages):
            report["errors"].append(f"{len(pages) - count} page(s) manquante(s) : {count} dans le PDF, "
                                    f"{len(pages)} attendue(s).")
        elif count > len(pages):
            report["errors"].append(f"{count - len(pages)} page(s) en trop : {count} dans le PDF, "
                                    f"{len(pages)} attendue(s).")
        for i, page in enumerate(pages):
            index = project.page_index(page.id)
            entry = {"index": index, "page_id": page.id, "present": i < count, "size_ok": None, "text_chars": 0,
                     "text_similarity": None, "images_found": 0,
                     "images_expected": _expected_images(page, project, mode),
                     "reconstructed": mode == "reconstruction" and bool(_displayed(page, project.render))}
            report["pages"].append(entry)
            if i >= count:
                continue
            pdf_page = pdf[i]
            try:
                width, height = pdf_page.get_size()
                entry["width_pt"], entry["height_pt"] = round(width, 2), round(height, 2)
                sheet = _sheet(project.render, page)
                if sheet is not None:
                    entry["size_ok"] = (abs(width - sheet.page_width) <= SIZE_TOLERANCE_PT
                                        and abs(height - sheet.page_height) <= SIZE_TOLERANCE_PT)
                    if not entry["size_ok"]:
                        report["errors"].append(f"Page {index + 1} : taille {width:.0f} × {height:.0f} pt, "
                                                f"attendue {sheet.page_width:.0f} × {sheet.page_height:.0f} pt.")
                textpage = pdf_page.get_textpage()
                try:
                    text = textpage.get_text_range()
                finally:
                    textpage.close()
                entry["text_chars"] = len(re.sub(r"\s+", "", text))
                expected = _expected_text(page, project, mode)
                if expected is not None and expected.strip():
                    entry["text_similarity"] = round(similarity(expected, text), 4)
                    if entry["text_similarity"] < TEXT_OK:
                        report["warnings"].append(f"Page {index + 1} : texte lu différent du texte attendu "
                                                  f"(similarité {entry['text_similarity']:.2f}).")
                entry["images_found"] = _count_images(pdf_page)
                if entry["images_found"] < entry["images_expected"]:
                    report["errors"].append(f"Page {index + 1} : {entry['images_found']} image(s) trouvée(s), "
                                            f"{entry['images_expected']} attendue(s).")
                if mode == "reconstruction" and not entry["reconstructed"]:
                    report["warnings"].append(f"Page {index + 1} : page blanche, non reconstruite.")
            finally:
                pdf_page.close()
    finally:
        pdf.close()
    present = [p for p in report["pages"] if p["present"]]
    sims = [p["text_similarity"] for p in present if p["text_similarity"] is not None]
    report["summary"] = {
        "text_similarity_mean": round(sum(sims) / len(sims), 4) if sims else None,
        "text_similarity_min": round(min(sims), 4) if sims else None,
        "empty_pages": [p["index"] for p in present if p["text_chars"] == 0 and p["images_found"] == 0],
        "blank_pages": [p["index"] for p in present if mode == "reconstruction" and not p["reconstructed"]],
        "images_expected": sum(p["images_expected"] for p in report["pages"]),
        "images_found": sum(p["images_found"] for p in present),
        "pages_size_ok": sum(1 for p in present if p["size_ok"]),
        "text_chars": sum(p["text_chars"] for p in present),
    }
    report["ok"] = not report["errors"]
    return report
