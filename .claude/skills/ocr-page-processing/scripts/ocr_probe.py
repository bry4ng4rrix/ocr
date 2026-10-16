"""Diagnostic OCR d'une image de page, sans dépendre du code de l'application.

Usage : python ocr_probe.py IMAGE [--lang fra+eng] [--save-debug DOSSIER]
L'image source n'est jamais modifiée : tout le travail se fait sur une copie en mémoire.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import cv2
import numpy as np

LOW_CONFIDENCE = 60
OSD_MAX_SIDE = 1600


def run_tesseract(image: np.ndarray, args: list[str], timeout: int = 180) -> tuple[str, str]:
    """Run tesseract on a PNG copy of `image`; return (stdout, stderr)."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "page.png"
        cv2.imwrite(str(path), image)
        done = subprocess.run(["tesseract", str(path), "stdout", *args],
                              capture_output=True, text=True, timeout=timeout)
    return done.stdout, done.stderr


def downscale(gray: np.ndarray, max_side: int) -> np.ndarray:
    scale = max_side / max(gray.shape[:2])
    if scale >= 1:
        return gray
    return cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)


def probe_osd(gray: np.ndarray) -> dict:
    out, err = run_tesseract(downscale(gray, OSD_MAX_SIDE), ["--psm", "0"])
    result: dict = {"raw_error": err.strip()[-200:] if not out else ""}
    for line in out.splitlines():
        key, _, value = line.partition(":")
        result[key.strip()] = value.strip()
    return result


def probe_ocr(gray: np.ndarray, lang: str) -> dict:
    out, err = run_tesseract(gray, ["-l", lang, "--psm", "3", "--dpi", "300", "tsv"])
    rows = list(csv.DictReader(io.StringIO(out), delimiter="\t", quoting=csv.QUOTE_NONE))
    words = [r for r in rows if r.get("level") == "5" and (r.get("text") or "").strip()]
    confidences = [float(w["conf"]) for w in words if float(w["conf"]) >= 0]
    lines = {(w["block_num"], w["par_num"], w["line_num"]) for w in words}
    paragraphs = {(w["block_num"], w["par_num"]) for w in words}
    low = [w for w in words if 0 <= float(w["conf"]) < LOW_CONFIDENCE]
    return {
        "words": len(words),
        "lines": len(lines),
        "paragraphs": len(paragraphs),
        "mean_confidence": round(sum(confidences) / len(confidences), 1) if confidences else None,
        "low_confidence_words": [
            {"text": w["text"], "conf": float(w["conf"]),
             "box": [int(w["left"]), int(w["top"]), int(w["width"]), int(w["height"])]}
            for w in low[:40]
        ],
        "low_confidence_count": len(low),
        "text_sample": " ".join(w["text"] for w in words[:60]),
        "stderr": err.strip()[-300:],
        "_words": words,
    }


def save_debug(image: np.ndarray, words: list[dict], folder: Path, stem: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    canvas = image.copy()
    for w in words:
        conf = float(w["conf"])
        colour = (0, 160, 0) if conf >= 80 else (0, 165, 255) if conf >= LOW_CONFIDENCE else (0, 0, 255)
        x, y, ww, hh = (int(w[k]) for k in ("left", "top", "width", "height"))
        cv2.rectangle(canvas, (x, y), (x + ww, y + hh), colour, 2)
    target = folder / f"{stem}_ocr_debug.png"
    cv2.imwrite(str(target), canvas)
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path)
    parser.add_argument("--lang", default="fra+eng")
    parser.add_argument("--save-debug", type=Path)
    options = parser.parse_args()

    if shutil.which("tesseract") is None:
        print(json.dumps({"error": "tesseract introuvable dans le PATH"}))
        return 2
    image = cv2.imread(str(options.image), cv2.IMREAD_COLOR)
    if image is None:
        print(json.dumps({"error": f"image illisible : {options.image}"}))
        return 2
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

    report: dict = {"image": str(options.image), "size": [image.shape[1], image.shape[0]], "lang": options.lang}
    start = time.perf_counter()
    report["osd"] = probe_osd(gray)
    report["osd_seconds"] = round(time.perf_counter() - start, 2)
    start = time.perf_counter()
    ocr = probe_ocr(gray, options.lang)
    report["ocr_seconds"] = round(time.perf_counter() - start, 2)
    words = ocr.pop("_words")
    report["ocr"] = ocr
    if options.save_debug:
        report["debug_image"] = str(save_debug(image, words, options.save_debug, options.image.stem))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
