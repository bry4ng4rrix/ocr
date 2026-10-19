"""Écran « Prix » : prix par type de page et montant de la numérisation, par livre et pour tous les livres.

Seules les pages dont le traitement est terminé (ou validées) sont comptées, selon leur type : texte seul,
avec tableau(x), avec image(s), couverture avec image. Le type est détecté automatiquement et corrigible dans l'écran
« Numérisation intelligente ».
"""
from __future__ import annotations

import streamlit as st

from components import common
from components.price_summary import money, price_summary
from models.errors import BookOcrError
from models.pricing import PricingSettings
from models.scanning import PAGE_KIND_LABELS, PageKind
from services import pricing_service, project_service, scanning_service

prices = pricing_service.load_prices()

st.subheader("Prix par page")
with st.form("pricing", border=True):
    currency = st.text_input("Monnaie", value=prices.currency, max_chars=12)
    columns = st.columns(4)
    text_price = columns[0].number_input("Page de texte seul", min_value=0.0, value=float(prices.text_page),
                                         step=50.0, format="%.2f")
    table_price = columns[1].number_input("Page avec tableau(x)", min_value=0.0, value=float(prices.table_page),
                                          step=50.0, format="%.2f")
    image_price = columns[2].number_input("Page avec image(s)", min_value=0.0, value=float(prices.image_page),
                                          step=50.0, format="%.2f")
    cover_price = columns[3].number_input("Couverture avec image", min_value=0.0, value=float(prices.cover_page),
                                          step=50.0, format="%.2f")
    st.caption("Une page qui contient plusieurs éléments prend le type prioritaire : couverture, puis tableau, "
               "puis image, puis texte seul. Le type de chaque page reste corrigible dans « Retoucher la page ».")
    saved = st.form_submit_button("Enregistrer les prix", type="primary", icon=":material/save:")
if saved:
    with common.user_action("Prix enregistrés.") as result:
        pricing_service.save_prices(PricingSettings(currency=currency.strip() or "Ar", text_page=text_price,
                                                    table_page=table_price, image_page=image_price,
                                                    cover_page=cover_price))
    if result.ok:
        st.rerun()

st.subheader("Montant de la numérisation")
st.caption("Pages terminées seulement (traitement fini ou page validée), comptées selon leur type.")
estimates, rows = [], []
with st.spinner("Calcul des statistiques des livres…"):
    for summary in project_service.list_projects():
        try:
            book = project_service.open_project(summary.id)
        except BookOcrError as error:
            st.warning(f"{summary.name} : {error.user_message}")
            continue
        stats = scanning_service.book_statistics(book)
        estimate = pricing_service.estimate(stats, prices)
        estimates.append(estimate)
        counts = {line.kind: line.pages for line in estimate.lines}
        rows.append({"Livre": book.name, "Pages": stats.total_pages, "Terminées": stats.completed_pages,
                     **{PAGE_KIND_LABELS[kind]: counts[kind] for kind in PageKind},
                     "Non terminées": stats.total_pages - stats.completed_pages,
                     "Montant": money(estimate.total, prices.currency)})
if not rows:
    st.info("Aucun livre pour l'instant.", icon=":material/menu_book:")
    st.stop()
st.dataframe(rows, hide_index=True, alt="Montant de la numérisation par livre")
st.markdown("**Tous les livres**")
price_summary(estimates, prices)
