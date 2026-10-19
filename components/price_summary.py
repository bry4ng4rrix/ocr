"""Bilan des pages terminées par type (texte seul, tableau, image, couverture) et montants, avec totaux."""
from __future__ import annotations

import streamlit as st

from models.pricing import BookPriceEstimate, PricingSettings
from models.scanning import PAGE_KIND_LABELS, PageKind


def money(value: float, currency: str) -> str:
    text = f"{value:,.2f}".replace(",", " ").replace(".", ",")
    if text.endswith(",00"):
        text = text[:-3]
    return f"{text} {currency}".strip()


def price_summary(estimates: list[BookPriceEstimate], prices: PricingSettings) -> None:
    """Une ligne de métriques par type de page, puis le total des pages et le total des prix."""
    counts = {kind: sum(line.pages for estimate in estimates for line in estimate.lines if line.kind == kind)
              for kind in PageKind}
    amounts = {kind: sum(line.amount for estimate in estimates for line in estimate.lines if line.kind == kind)
               for kind in PageKind}
    total_pages = sum(counts.values())
    total_amount = sum(amounts.values())
    with st.container(horizontal=True):
        for kind in PageKind:
            st.metric(PAGE_KIND_LABELS[kind], f"{counts[kind]} page(s)", border=True,
                      help=f"Prix unitaire : {money(prices.price(kind), prices.currency)}")
        st.metric("Total des pages", f"{total_pages} page(s)", border=True)
    with st.container(horizontal=True):
        for kind in PageKind:
            st.metric(f"Prix · {PAGE_KIND_LABELS[kind].lower()}", money(amounts[kind], prices.currency), border=True,
                      help=f"{counts[kind]} × {money(prices.price(kind), prices.currency)}")
        st.metric("Total des prix", money(total_amount, prices.currency), border=True)
    unclassified = sum(estimate.unclassified_pages for estimate in estimates)
    if unclassified:
        st.caption(f"{unclassified} page(s) terminée(s) sans type connu ne sont pas comptées.")
    if not any((prices.text_page, prices.table_page, prices.image_page, prices.cover_page)):
        st.caption("Aucun prix n'est encore défini : réglez-les dans l'onglet « Prix ».")
