"""OCR processing: languages, ProcessingSettings, page choice, explicit launch with progress, results."""
from __future__ import annotations

import streamlit as st

from core.document_models import PageStatus, ProcessingMode, ProcessingSettings

from . import overview_page, state

MODE_LABELS = {
    ProcessingMode.AUTO.value: "Auto (choisi d'après le contenu de la page)",
    ProcessingMode.TEXT.value: "A — Texte et OCR",
    ProcessingMode.ILLUSTRATION.value: "B — Illustrations et couleurs",
    ProcessingMode.MIXED.value: "C — Page mixte",
}


def render(prj) -> None:
    st.header("Traitement OCR")
    installed = overview_page.tesseract_panel()
    if not prj.pages:
        st.info("Importez d'abord des pages (rubrique « Pages »).")
        return
    settings = _settings(prj, installed)
    st.subheader("Pages à traiter")
    choice = st.radio("Pages", ["Pages non traitées ou à OCR obsolète", "Toutes les pages", "Sélection"],
                      key="ocr_scope", horizontal=True)
    if choice == "Toutes les pages":
        ids = [p.id for p in prj.pages]
    elif choice == "Sélection":
        ids = st.multiselect("Pages choisies", [p.id for p in prj.pages], key=state.k("ocr_pages"),
                             format_func=lambda pid: state.page_label(prj, prj.page(pid)))
    else:
        ids = [p.id for p in prj.pages if p.status != PageStatus.PROCESSED or p.ocr_outdated]
    redetect = st.checkbox("Refaire les détections automatiques (orientation, feuille, inclinaison)",
                           key="ocr_redetect", help="Sinon, les détections déjà faites sont réutilisées.")
    st.info("Relancer l'OCR conserve les blocs que vous avez corrigés, déplacés ou créés, ainsi que les "
            "illustrations remplacées ou retouchées.")
    can_run = bool(ids) and bool(settings.languages) and all(lang in installed for lang in settings.languages)
    if st.button(f"Lancer l'OCR ({len(ids)} page(s))", type="primary", disabled=not can_run, key="run_ocr"):
        _run(prj, ids, redetect)
    if not ids:
        st.caption("Aucune page à traiter avec ce choix.")
    _results(prj)


def _settings(prj, installed: list[str]) -> ProcessingSettings:
    s = prj.processing
    with st.expander("Réglages du traitement", expanded=True):
        options = sorted(set(installed) | set(s.languages))
        langs = st.multiselect("Langues du livre (installées dans Tesseract)", options,
                               default=[lang for lang in s.languages if lang in options], key=state.k("ocr_langs"),
                               help="Choisissez toutes les langues présentes : l'ordre compte peu.")
        for lang in ("fra", "eng"):
            if lang not in installed:
                st.warning(f"La langue « {lang} » n'est pas installée dans Tesseract "
                           f"(paquet tesseract-ocr-{lang} ou tesseract-data-{lang}).")
        not_installed = [lang for lang in langs if lang not in installed]
        if not_installed:
            st.error(f"Langue(s) choisie(s) non installée(s) : {', '.join(not_installed)}.")
        c = st.columns(2)
        auto_orientation = c[0].checkbox("Orientation automatique (OSD)", value=s.auto_orientation,
                                         key=state.k("ps_orient"))
        auto_crop = c[0].checkbox("Recadrage automatique de la feuille", value=s.auto_crop, key=state.k("ps_crop"))
        auto_deskew = c[0].checkbox("Redressement automatique", value=s.auto_deskew, key=state.k("ps_deskew"))
        detect = c[1].checkbox("Détecter les illustrations", value=s.detect_illustrations, key=state.k("ps_illus"))
        retry = c[1].checkbox("Seconde lecture binarisée si la confiance est faible", value=s.retry_binarized,
                              key=state.k("ps_retry"))
        dehyphen = c[1].checkbox("Recoller les mots coupés en fin de ligne", value=s.dehyphenate,
                                 key=state.k("ps_dehyphen"))
        low = st.slider("Seuil de faible confiance (mots et blocs « à vérifier »)", 0, 100,
                        int(s.low_confidence), key=state.k("ps_low"))
        modes = list(MODE_LABELS)
        mode = st.selectbox("Mode de traitement par défaut des nouvelles pages", modes,
                            index=modes.index(s.default_mode.value), format_func=MODE_LABELS.get,
                            key=state.k("ps_mode"))
        new = ProcessingSettings(languages=langs, default_mode=ProcessingMode(mode), auto_orientation=auto_orientation,
                                 auto_crop=auto_crop, auto_deskew=auto_deskew, detect_illustrations=detect,
                                 retry_binarized=retry, dehyphenate=dehyphen, low_confidence=float(low))
        if new != s:
            prj.processing = new          # project-level settings: saved at once (no page history)
            state.save(prj)
        st.caption("Les cases d'orientation, de recadrage et de redressement s'appliquent aux pages importées "
                   "ensuite ; pour une page déjà importée, utilisez l'onglet Géométrie de l'éditeur d'image.")
    return prj.processing


def _run(prj, ids: list[str], redetect: bool) -> None:
    bar = st.progress(0.0, text="Préparation…")
    with state.guard("OCR"), st.status(f"OCR de {len(ids)} page(s)…", expanded=True) as status:
        last = {"page": -1}

        def progress(index: int, total: int, fraction: float, message: str) -> None:
            overall = (index + fraction) / max(total, 1)
            bar.progress(min(overall, 1.0), text=f"Page {index + 1}/{total} — {message}")
            if index != last["page"]:
                status.write(f"Page {index + 1}/{total}")
                last["page"] = index

        results = state.processing().process_pages(prj, ids, progress=progress, redetect=redetect)
        st.session_state["ocr_results"] = (prj.id, [r.__dict__ for r in results])
        failed = sum(1 for r in results if not r.ok)
        status.update(label=f"OCR terminé : {len(results) - failed} page(s) traitée(s), {failed} en erreur",
                      state="error" if failed else "complete")
        bar.progress(1.0, text="Terminé")
        state.bump()


def _results(prj) -> None:
    stored = st.session_state.get("ocr_results")
    if not stored or stored[0] != prj.id:
        return
    st.subheader("Résultats du dernier traitement")
    rows = []
    for r in stored[1]:
        page = prj.page(r["page_id"])
        rows.append({"Page": state.page_label(prj, page) if page else r["page_id"],
                     "Réussi": "oui" if r["ok"] else "non", "Blocs texte": r["text_blocks"],
                     "Illustrations": r["image_blocks"], "Durée (s)": round(r["seconds"], 1),
                     "Erreur": r["message"], "Avertissements": " ; ".join(r["warnings"])})
    st.dataframe(rows, hide_index=True)
