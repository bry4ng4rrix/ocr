"""Numérisation de fac-similés image avec validation page par page."""
from __future__ import annotations

import uuid

import cv2
import numpy as np
import streamlit as st

from services import image_service, image_enhancement, ollama_vision, page_detection, pdf_export


def _init() -> None:
    st.session_state.setdefault("scan_pages", [])


def _add_page(image: np.ndarray, source: str, source_order: int, index: int,
              corners: tuple[tuple[float, float], ...] | None = None,
              confidence: float = 0.0, warning: str = "") -> dict:
    return {"id": uuid.uuid4().hex, "source": source, "source_order": source_order,
            "source_index": index, "original": image, "corners": corners,
            "confidence": confidence, "warning": warning, "crop": None,
            "enhanced": None, "validated": False, "rotation": 0}


def _load_files(files, dpi: int) -> list[dict]:
    added = []
    for file_order, uploaded in enumerate(files):
        data = uploaded.getvalue()
        items = image_service.inspect_upload(uploaded.name, data)
        for item in items:
            image = (image_service.render_pdf_page(data, item.index, dpi)
                     if item.kind == "pdf" else image_service.decode_image(data, item.index, uploaded.name))
            added.append(_add_page(image, uploaded.name, file_order, item.index))
    return added


def _render_detection(page: dict, source_index: int, split: float) -> None:
    candidates = page_detection.detect_pages(page["original"])
    if len(candidates) == 2:
        halves = page_detection.split_spread(page["original"], split)
        source = page["original"]
        # Replace the current source entry with independent physical pages.
        pages = st.session_state.scan_pages
        position = next(i for i, value in enumerate(pages) if value["id"] == page["id"])
        left = _add_page(halves[0], page["source"], page["source_order"], page["source_index"],
                         confidence=candidates[0].confidence, warning=candidates[0].warning)
        right = _add_page(halves[1], page["source"], page["source_order"], page["source_index"],
                          confidence=candidates[1].confidence, warning=candidates[1].warning)
        pages[position:position+1] = [left, right]
        return
    candidate = candidates[0]
    page["corners"] = candidate.corners
    page["confidence"] = candidate.confidence
    page["warning"] = candidate.warning
    page["crop"] = page_detection.warp_page(page["original"], candidate.corners)
    page["enhanced"] = None


_init()
st.title("Numérisation et amélioration PDF")
st.caption("Chaque page physique validée devient une feuille image distincte dans le PDF final.")

with st.expander("Ollama et Gemma 3:4b", expanded=False):
    st.caption("Analyse locale facultative. Les images ne sont envoyées qu’au serveur Ollama configuré.")
    if st.button("Tester la connexion Ollama", key="scan_ollama_test"):
        try:
            st.success(ollama_vision.test_connection())
        except (OSError, RuntimeError, ValueError) as error:
            st.error(str(error))

controls, uploader_col = st.columns([1, 2])
with controls:
    dpi = st.slider("Résolution des pages PDF (DPI)", 150, 500, 225, 25, key="scan_dpi")
    mode = st.selectbox("Mode d’amélioration", ["Automatique", "Document couleur", "Document couleur amélioré",
                                                  "Document noir et blanc"], key="scan_mode")
    strength = st.slider("Intensité", 0, 70, 30, key="scan_strength")
with uploader_col:
    uploads = st.file_uploader("Importer des images ou des PDF", type=["jpg", "jpeg", "png", "tif", "tiff", "pdf"],
                               accept_multiple_files=True, key="scan_upload")
    if st.button("Ajouter les fichiers", disabled=not uploads, key="scan_add"):
        try:
            with st.spinner("Lecture progressive des fichiers…"):
                st.session_state.scan_pages.extend(_load_files(uploads, dpi))
            st.success("Import terminé.")
        except (ValueError, OSError, RuntimeError) as error:
            st.error(f"Import impossible : {error}")

pages: list[dict] = st.session_state.scan_pages
st.metric("Pages chargées", len(pages))
if pages:
    selected_id = st.selectbox("Page à modifier", [p["id"] for p in pages],
        format_func=lambda pid: f"{next(i for i,p in enumerate(pages) if p['id']==pid)+1}. "
        f"{next(p for p in pages if p['id']==pid)['source']} — source {next(p for p in pages if p['id']==pid)['source_index']+1}",
        key="scan_selected")
    page = next(p for p in pages if p["id"] == selected_id)
    left, right = st.columns(2)
    with left:
        st.image(image_service.thumbnail(page["original"]), caption="Image d’origine", alt="Image source")
    with right:
        preview = page["enhanced"] if page["enhanced"] is not None else page["crop"]
        st.image(image_service.thumbnail(preview if preview is not None else page["original"]),
                 caption="Résultat courant" if preview is not None else "En attente de détection",
                 alt="Aperçu de la page")
    if page["warning"]:
        st.warning(f"Confiance {page['confidence']:.0%}. {page['warning']}")
    act, split_control = st.columns([1, 2])
    split_x = split_control.slider("Position de la reliure proposée", 0.2, 0.8, 0.5, 0.01,
                                   key=f"split_{page['id']}")
    if act.button("Détecter et recadrer", key=f"detect_{page['id']}"):
        _render_detection(page, pages.index(page), split_x)
        st.rerun()
    if act.button("Séparer les doubles pages", key=f"split_button_{page['id']}"):
        halves = page_detection.split_spread(page["original"], split_x)
        pos = pages.index(page)
        pages[pos:pos+1] = [_add_page(half, page["source"], page["source_order"], page["source_index"])
                            for half in halves]
        st.rerun()
    current = page["crop"] if page["crop"] is not None else page["original"]
    h, w = page["original"].shape[:2]
    st.caption("Ajustement manuel rectangulaire relatif à l’image source ; gardez une petite marge autour du contenu.")
    c1, c2 = st.columns(2)
    x0 = c1.slider("Bord gauche (%)", 0, 45, 0, key=f"x0_{page['id']}")
    x1 = c2.slider("Bord droit (%)", 55, 100, 100, key=f"x1_{page['id']}")
    y0 = c1.slider("Bord haut (%)", 0, 45, 0, key=f"y0_{page['id']}")
    y1 = c2.slider("Bord bas (%)", 55, 100, 100, key=f"y1_{page['id']}")
    if st.button("Appliquer le cadrage manuel", key=f"manual_{page['id']}"):
        source = page["original"]
        page["crop"] = source[int(y0*h/100):max(int(y1*h/100), 1), int(x0*w/100):max(int(x1*w/100), 1)].copy()
        page["enhanced"] = None
    rotate = st.selectbox("Rotation manuelle", [0, 90, 180, 270], key=f"rotation_{page['id']}")
    if st.button("Faire pivoter", key=f"rotate_{page['id']}") and page["crop"] is not None:
        page["crop"] = np.rot90(page["crop"], k=-(rotate//90)).copy()
        page["enhanced"] = None
    if st.button("Refaire l’amélioration", key=f"enhance_{page['id']}"):
        base = page["crop"] if page["crop"] is not None else page["original"]
        page["enhanced"] = image_enhancement.enhance_page(base, mode, strength)
    if st.button("Revenir à l’original", key=f"reset_{page['id']}"):
        page["crop"] = None
        page["enhanced"] = None
    if st.button("Valider la page", key=f"validate_{page['id']}"):
        page["validated"] = True
        if page["crop"] is None:
            page["crop"] = page["original"].copy()
        if page["enhanced"] is None:
            page["enhanced"] = image_enhancement.enhance_page(page["crop"], mode, strength)
    remove = st.checkbox("Confirmer la suppression de cette page", key=f"confirm_delete_{page['id']}")
    if st.button("Supprimer cette page", disabled=not remove, key=f"delete_{page['id']}"):
        st.session_state.scan_pages = [p for p in pages if p["id"] != selected_id]
        st.rerun()
    if st.button("Analyser avec Gemma", key=f"gemma_{page['id']}"):
        try:
            from services.image_service import encode_png
            page["advice"] = ollama_vision.analyze(encode_png(current))
            st.json(page["advice"].model_dump())
        except (OSError, RuntimeError, ValueError) as error:
            st.error(str(error))

    order_rows = [{"id": p["id"], "ordre": index, "source": p["source"],
                   "validée": p["validated"]} for index, p in enumerate(pages, 1)]
    edited = st.data_editor(order_rows, hide_index=True, disabled=["id", "source", "validée"],
                            column_config={"ordre": st.column_config.NumberColumn("Ordre", min_value=1,
                                                                                   max_value=len(pages), step=1)},
                            key="scan_order")
    if st.button("Appliquer l’ordre", key="scan_reorder"):
        rank = {row["id"]: row["ordre"] for row in edited}
        pages.sort(key=lambda p: (rank[p["id"]], p["id"]))
        st.rerun()
    paper = st.selectbox("Format PDF", ["Format réel", "A4"], key="scan_paper")
    quality = st.slider("Qualité d’image JPEG du PDF", 65, 100, 92, key="scan_quality")
    selected = [p for p in pages if p["validated"] and p["enhanced"] is not None]
    if len(selected) != sum(1 for p in pages if p["validated"]):
        st.warning("Chaque page validée doit être traitée avant l’export.")
    if selected:
        pdf_data = pdf_export.create_image_pdf([p["enhanced"] for p in selected], dpi=dpi,
                                               quality=quality, paper=paper)
        st.download_button("Exporter le PDF", pdf_data, "livre_numerise.pdf", "application/pdf",
                           key="scan_download_pdf")
        zip_data = pdf_export.create_pages_zip([(p["source"], p["enhanced"]) for p in selected])
        st.download_button("Exporter aussi les pages en ZIP", zip_data, "pages_ameliorees.zip",
                           "application/zip", key="scan_download_zip")
    if st.button("Réinitialiser cette session", key="scan_clear"):
        st.session_state.scan_pages = []
        st.rerun()
