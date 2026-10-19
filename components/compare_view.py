"""Comparateur côte à côte (original, corrigé, reconstruit…) avec éléments natifs Streamlit."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import streamlit as st
from PIL import Image


@dataclass
class Panel:
    title: str
    image: np.ndarray | Image.Image | None
    missing: str = "Pas encore disponible."
    caption: str | None = None


def compare_view(panels: list[Panel], *, border: bool = True) -> None:
    """Affiche les images dans des colonnes de même largeur ; un message remplace une image absente."""
    columns = st.columns(len(panels), border=border)
    for column, panel in zip(columns, panels):
        with column:
            st.markdown(f"**{panel.title}**")
            if panel.image is None:
                st.info(panel.missing, icon=":material/image_not_supported:")
            else:
                st.image(panel.image, width="stretch", alt=panel.title)
            if panel.caption:
                st.caption(panel.caption)
