"""Contrôle d'un fichier PDF avec PyMuPDF (lecture seule).

Usage : python check_pdf.py FICHIER.pdf [--expect-pages N] [--expect-text MOT ...] [--render DOSSIER]
Code de sortie : 0 conforme, 1 attente non satisfaite, 2 fichier illisible ou PyMuPDF absent.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BLANK_DPI = 40
BLANK_INK_RATIO = 0.002
MM_PER_PT = 25.4 / 72


def is_visually_blank(page) -> bool:
    pix = page.get_pixmap(dpi=BLANK_DPI, colorspace="gray", alpha=False)
    samples = pix.samples
    dark = sum(1 for value in samples if value < 235)
    return dark / max(1, len(samples)) < BLANK_INK_RATIO


def inspect(path: Path, render_dir: Path | None) -> dict:
    import pymupdf

    document = pymupdf.open(path)
    pages = []
    full_text = []
    for page in document:
        text = page.get_text("text")
        full_text.append(text)
        fonts = page.get_fonts(full=True)
        info = {
            "number": page.number + 1,
            "size_mm": [round(page.rect.width * MM_PER_PT, 1), round(page.rect.height * MM_PER_PT, 1)],
            "chars": len(text.strip()),
            "images": len(page.get_images(full=True)),
            # get_fonts: (xref, ext, type, basefont, name, encoding, ...); ext "n/a" = police non intégrée
            "fonts": sorted({f"{f[3]}{'' if f[1] != 'n/a' else ' (non intégrée)'}" for f in fonts}),
            "blank": is_visually_blank(page),
        }
        if render_dir is not None:
            render_dir.mkdir(parents=True, exist_ok=True)
            target = render_dir / f"{path.stem}_p{page.number + 1:03d}.png"
            page.get_pixmap(dpi=100).save(target)
            info["render"] = str(target)
        pages.append(info)
    report = {
        "file": str(path),
        "page_count": document.page_count,
        "encrypted": document.is_encrypted,
        "pages": pages,
        "pages_without_text": [p["number"] for p in pages if p["chars"] == 0],
        "blank_pages": [p["number"] for p in pages if p["blank"]],
        "non_embedded_fonts": sorted({f for p in pages for f in p["fonts"] if "non intégrée" in f}),
        "_text": "\n".join(full_text),
    }
    document.close()
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--expect-pages", type=int)
    parser.add_argument("--expect-text", nargs="*", default=[])
    parser.add_argument("--render", type=Path)
    options = parser.parse_args()
    try:
        report = inspect(options.pdf, options.render)
    except ImportError:
        print(json.dumps({"error": "PyMuPDF absent : .venv/bin/pip install pymupdf"}))
        return 2
    except (OSError, RuntimeError, ValueError) as error:
        print(json.dumps({"file": str(options.pdf), "error": f"{type(error).__name__}: {error}"}, ensure_ascii=False))
        return 2

    text = report.pop("_text")
    failures = []
    if options.expect_pages is not None and report["page_count"] != options.expect_pages:
        failures.append(f"pages {report['page_count']} ≠ {options.expect_pages} attendues")
    missing = [t for t in options.expect_text if t not in text]
    if missing:
        failures.append(f"texte absent : {missing}")
    if report["blank_pages"]:
        failures.append(f"pages visuellement blanches : {report['blank_pages']}")
    report["failures"] = failures
    report["ok"] = not failures
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
