"""Vignettes triables par glisser-déposer (Streamlit Custom Components v2).

Le composant affiche les pages dans l'ordre reçu ; l'utilisateur les déplace à la souris (glisser-déposer)
ou au clavier (focus sur une vignette, Alt + flèche gauche/droite). Chaque nouvel ordre est envoyé par :

    setTriggerValue("order", {ids: [...], nonce})

Rien n'est enregistré ici : l'écran appelle project_service.reorder_pages. Repli accessible sans souris :
« Déplacer vers la position » dans l'écran Pages.
"""
from __future__ import annotations

from typing import Any

import streamlit as st

_HTML = """
<div class="sp-root"></div>
"""

_CSS = """
.sp-root { font-family: var(--st-font, sans-serif); font-size: 12px; color: var(--st-text-color, #222); }
.sp-hint { color: #667; margin-bottom: 6px; }
.sp-grid { display: flex; flex-wrap: wrap; gap: 8px; }
.sp-card { width: 118px; border: 1px solid #c9d1d9; border-radius: 6px; padding: 4px; background: #fff;
  cursor: grab; text-align: center; }
.sp-card:focus { outline: 3px solid var(--st-primary-color, #1f6f8b); }
.sp-card.dragging { opacity: 0.4; }
.sp-card.target-before { box-shadow: -4px 0 0 0 var(--st-primary-color, #1f6f8b); }
.sp-card.target-after { box-shadow: 4px 0 0 0 var(--st-primary-color, #1f6f8b); }
.sp-card img { width: 108px; height: 140px; object-fit: contain; background: #f3f3f3; display: block;
  margin: 0 auto 3px auto; pointer-events: none; }
.sp-card .num { font-weight: 600; }
.sp-card .name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
"""

_JS = r"""
function nonce() { return Date.now().toString(36) + Math.random().toString(36).slice(2, 8); }

export default function (component) {
  const { data, setTriggerValue, parentElement } = component;
  const root = parentElement.querySelector(".sp-root");
  if (!root || !data) return;
  root.replaceChildren();
  const order = (data.items || []).map((item) => ({ ...item }));
  let dragged = null;

  const hint = document.createElement("div");
  hint.className = "sp-hint";
  hint.textContent = "Glissez une vignette vers sa nouvelle place, ou sélectionnez-la (Tab) "
    + "puis Alt + flèche gauche / droite.";
  const grid = document.createElement("div");
  grid.className = "sp-grid";
  grid.setAttribute("role", "list");
  grid.setAttribute("aria-label", "Ordre des pages");
  root.append(hint, grid);

  function emit(focusId) {
    setTriggerValue("order", { ids: order.map((item) => item.id), nonce: nonce() });
    render(focusId);
  }

  function moveItem(from, to) {
    if (from === to || from < 0 || to < 0 || from >= order.length || to > order.length) return false;
    const [item] = order.splice(from, 1);
    order.splice(to > from ? to - 1 : to, 0, item);
    return true;
  }

  function render(focusId) {
    grid.replaceChildren();
    order.forEach((item, index) => {
      const card = document.createElement("div");
      card.className = "sp-card";
      card.draggable = true;
      card.tabIndex = 0;
      card.setAttribute("role", "listitem");
      card.setAttribute("aria-label", `Page ${index + 1} : ${item.label}`);
      card.dataset.id = item.id;
      const img = document.createElement("img");
      img.src = item.thumb || "";
      img.alt = "";
      const num = document.createElement("div"); num.className = "num"; num.textContent = String(index + 1);
      const name = document.createElement("div"); name.className = "name"; name.textContent = item.label;
      name.title = item.label;
      card.append(img, num, name);

      card.ondragstart = (e) => { dragged = index; card.classList.add("dragging");
        e.dataTransfer.effectAllowed = "move"; e.dataTransfer.setData("text/plain", item.id); };
      card.ondragend = () => { card.classList.remove("dragging"); dragged = null;
        grid.querySelectorAll(".sp-card").forEach((c) => c.classList.remove("target-before", "target-after")); };
      card.ondragover = (e) => {
        if (dragged === null) return;
        e.preventDefault();
        const r = card.getBoundingClientRect();
        const after = e.clientX > r.left + r.width / 2;
        card.classList.toggle("target-after", after);
        card.classList.toggle("target-before", !after);
      };
      card.ondragleave = () => card.classList.remove("target-before", "target-after");
      card.ondrop = (e) => {
        e.preventDefault();
        if (dragged === null) return;
        const r = card.getBoundingClientRect();
        const after = e.clientX > r.left + r.width / 2;
        const from = dragged;
        dragged = null;
        if (moveItem(from, after ? index + 1 : index)) emit(item.id);
        else render();
      };
      card.onkeydown = (e) => {
        if (!e.altKey || (e.key !== "ArrowLeft" && e.key !== "ArrowRight")) return;
        e.preventDefault();
        const to = e.key === "ArrowLeft" ? index - 1 : index + 2;
        if (to < 0 || to > order.length) return;
        if (moveItem(index, to)) emit(item.id);
      };
      grid.appendChild(card);
      if (focusId && item.id === focusId) card.focus({ preventScroll: true });
    });
  }
  render(null);
}
"""

_SORTABLE = st.components.v2.component("bookocr_sortable_pages", html=_HTML, css=_CSS, js=_JS)


def sortable_pages(items: list[dict[str, Any]], *, key: str) -> list[str] | None:
    """`items` : [{id, label, thumb (data URI)}] dans l'ordre actuel. Renvoie le nouvel ordre (une seule fois
    par action de l'utilisateur) ou None."""
    result = _SORTABLE(key=key, data={"items": items}, on_order_change=lambda: None, height="content")
    event = getattr(result, "order", None)
    if not isinstance(event, dict) or not event.get("nonce") or not isinstance(event.get("ids"), list):
        return None
    seen = st.session_state.setdefault("_sortable_nonces", [])
    if event["nonce"] in seen:
        return None
    seen.append(event["nonce"])
    del seen[:-50]
    return [str(page_id) for page_id in event["ids"]]
