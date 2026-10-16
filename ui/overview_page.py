"""Overview: counts, confidence, blocks to check, warnings per page, Tesseract status."""
from __future__ import annotations

import streamlit as st

from core.document_models import PageStatus

from . import state


@st.cache_data(ttl=120, show_spinner=False)
def tesseract_status() -> tuple[str | None, list[str]]:
    from core import ocr_engine
    return ocr_engine.tesseract_version(), ocr_engine.available_languages()


def tesseract_panel() -> list[str]:
    """Tesseract version and languages, with install instructions when something is missing."""
    version, languages = tesseract_status()
    if not version:
        st.error("Tesseract est introuvable : l'OCR est impossible.\n\n"
                 "- Debian, Ubuntu : `sudo apt install tesseract-ocr tesseract-ocr-fra tesseract-ocr-eng`\n"
                 "- Arch, Manjaro : `sudo pacman -S tesseract tesseract-data-fra tesseract-data-eng`\n"
                 "- Ou indiquez le chemin du programme dans la variable d'environnement `LIVREOCR_TESSERACT`.")
        return []
    st.success(f"Tesseract {version} — langues installées : {', '.join(languages) or 'aucune'}")
    missing = [lang for lang in ("fra", "eng") if lang not in languages]
    if missing:
        st.warning(f"Langue(s) absente(s) : {', '.join(missing)}. Installez par exemple "
                   f"`tesseract-ocr-{missing[0]}` (Debian, Ubuntu) ou `tesseract-data-{missing[0]}` (Arch).")
    return languages


def render(prj) -> None:
    st.header(f"Vue d'ensemble — {prj.name}")
    pages = prj.pages
    processed = [p for p in pages if p.status == PageStatus.PROCESSED]
    errors = [p for p in pages if p.status == PageStatus.ERROR]
    outdated = [p for p in pages if p.ocr_outdated]
    text_blocks = sum(len(p.text_blocks) for p in pages)
    image_blocks = sum(len(p.image_blocks) for p in pages)
    review = sum(1 for p in pages for b in p.blocks if b.needs_review)
    confidences = [p.ocr_mean_confidence for p in processed if p.ocr_mean_confidence >= 0]
    mean_conf = sum(confidences) / len(confidences) if confidences else None

    row = st.columns(4)
    row[0].metric("Pages", len(pages))
    row[1].metric("Pages traitées", len(processed))
    row[2].metric("En erreur", len(errors))
    row[3].metric("OCR obsolète", len(outdated))
    row = st.columns(4)
    row[0].metric("Blocs de texte", text_blocks)
    row[1].metric("Illustrations", image_blocks)
    row[2].metric("Confiance moyenne", f"{mean_conf:.0f} %" if mean_conf is not None else "—")
    row[3].metric("Blocs à vérifier", review)

    st.subheader("Moteur OCR")
    tesseract_panel()

    if not pages:
        st.info("Ce projet ne contient encore aucune page : ouvrez la rubrique « Pages » pour importer "
                "des photos ou des PDF.")
        return
    st.subheader("Pages")
    rows = []
    for i, p in enumerate(pages, start=1):
        rows.append({
            "N°": i, "Fichier": p.original_filename, "Statut": state.status_text(p),
            "Blocs texte": len(p.text_blocks), "Illustrations": len(p.image_blocks),
            "À vérifier": sum(1 for b in p.blocks if b.needs_review),
            "Confiance": round(p.ocr_mean_confidence, 1) if p.ocr_mean_confidence >= 0 else None,
            "Avertissements": len(p.warnings),
        })
    st.dataframe(rows, hide_index=True)
    problems = [(i, p) for i, p in enumerate(pages, start=1) if p.warnings or p.error or p.ocr_outdated]
    if problems:
        st.subheader("Avertissements et erreurs")
        for i, p in problems:
            with st.expander(f"Page {i} — {p.original_filename}", expanded=bool(p.error)):
                if p.error:
                    st.error(p.error)
                if p.ocr_outdated:
                    st.warning("Le cadre de la page a changé depuis l'OCR : relancez l'OCR de cette page.")
                for w in p.warnings:
                    st.write(f"- {w}")
