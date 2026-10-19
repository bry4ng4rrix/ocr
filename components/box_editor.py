"""Éditeur de boîtes SVG (Streamlit Custom Components v2) : sélectionner, déplacer, redimensionner, dessiner.

Contrat du composant : le module JS reçoit `data` à chaque exécution et envoie un déclencheur par action :

    setTriggerValue("event", {action: "select" | "update" | "create", id, box: [x, y, w, h], nonce})

Les coordonnées sont normalisées 0..1 sur l'image affichée (= image corrigée de la page, ARCHITECTURE §4).
Le composant n'enregistre rien : l'écran applique l'événement par les services (une seule fois par `nonce`).
Repli accessible : les champs numériques de position et de taille de l'éditeur de page.
"""
from __future__ import annotations

from typing import Any

import streamlit as st

from models import Box, ImageBlock, Page, TextBlock

_HTML = """
<div class="be-root"></div>
"""

_CSS = """
.be-root { font-family: var(--st-font, sans-serif); font-size: 13px; color: var(--st-text-color, #222); }
.be-bar { display: flex; gap: 6px; align-items: center; margin-bottom: 6px; flex-wrap: wrap; }
.be-bar button { border: 1px solid #b8c2cc; background: #fff; border-radius: 6px; padding: 3px 10px;
  cursor: pointer; font: inherit; }
.be-bar button.on { background: var(--st-primary-color, #1f6f8b); color: #fff;
  border-color: var(--st-primary-color, #1f6f8b); }
.be-bar .hint { color: #667; }
.be-stage { position: relative; display: inline-block; line-height: 0; user-select: none; }
.be-stage img { display: block; max-width: none; }
.be-stage svg { position: absolute; left: 0; top: 0; width: 100%; height: 100%; touch-action: none; }
.be-stage svg.drawing { cursor: crosshair; }
.box rect.body { fill: rgba(31,111,139,0.06); stroke-width: 2; cursor: move; }
.box.text rect.body { stroke: #1f6f8b; }
.box.image rect.body { stroke: #2e8b57; fill: rgba(46,139,87,0.06); }
.box.hidden rect.body { stroke: #999; stroke-dasharray: 3 3; }
.box.review rect.body { stroke: #e67e00; stroke-width: 3; }
.box.selected rect.body { stroke: #d62828; stroke-width: 3; fill: rgba(214,40,40,0.08); }
.box rect.body:focus { outline: none; stroke: #d62828; }
.box rect.tag { fill: #1f6f8b; }
.box.image rect.tag { fill: #2e8b57; }
.box.review rect.tag { fill: #e67e00; }
.box.selected rect.tag { fill: #d62828; }
.box text { fill: #fff; font-size: 11px; font-family: sans-serif; pointer-events: none; }
rect.handle { fill: #fff; stroke: #d62828; stroke-width: 2; cursor: nwse-resize; }
rect.handle.h1, rect.handle.h3 { cursor: nesw-resize; }
rect.draft { fill: rgba(214,40,40,0.1); stroke: #d62828; stroke-dasharray: 4 3; stroke-width: 2; }
"""

_JS = r"""
const NS = "http://www.w3.org/2000/svg";
const MIN = 0.005;
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
function el(name, attrs, parent) {
  const node = document.createElementNS(NS, name);
  for (const k in attrs) node.setAttribute(k, attrs[k]);
  if (parent) parent.appendChild(node);
  return node;
}
function nonce() { return Date.now().toString(36) + Math.random().toString(36).slice(2, 8); }

export default function (component) {
  const { data, setTriggerValue, parentElement } = component;
  const root = parentElement.querySelector(".be-root");
  if (!root || !data) return;
  root.replaceChildren();
  const items = (data.boxes || []).map((b) => ({ ...b }));
  let selected = data.selected || null;
  let drawMode = false;
  let drag = null, draft = null, keyTimer = null;

  const bar = document.createElement("div");
  bar.className = "be-bar";
  const bSel = document.createElement("button"); bSel.type = "button"; bSel.textContent = "Sélectionner / déplacer";
  const bDraw = document.createElement("button"); bDraw.type = "button";
  bDraw.textContent = data.draw_label || "Dessiner un bloc";
  const hint = document.createElement("span"); hint.className = "hint";
  hint.textContent = "Clic : sélection · glisser : déplacer · coin : redimensionner · "
    + "flèches : déplacer (Maj : plus vite, Alt : taille)";
  if (data.allow_draw !== false && !data.readonly) { bar.append(bSel, bDraw); }
  bar.append(hint);
  root.appendChild(bar);

  const stage = document.createElement("div"); stage.className = "be-stage";
  const img = document.createElement("img");
  img.src = data.image; img.alt = data.alt || "Page";
  img.style.width = (data.width || 700) + "px";
  const svg = el("svg", { role: "group", "aria-label": "Blocs de la page" });
  stage.append(img, svg);
  root.appendChild(stage);

  function setDraw(on) {
    drawMode = on;
    svg.classList.toggle("drawing", on);
    bDraw.classList.toggle("on", on);
    bSel.classList.toggle("on", !on);
  }
  bSel.onclick = () => setDraw(false);
  bDraw.onclick = () => setDraw(!drawMode);
  setDraw(false);

  const size = () => { const r = img.getBoundingClientRect(); return [r.width || 1, r.height || 1]; };
  function point(e) {
    const r = img.getBoundingClientRect();
    return [clamp((e.clientX - r.left) / r.width, 0, 1), clamp((e.clientY - r.top) / r.height, 0, 1)];
  }
  const find = (id) => items.find((i) => String(i.id) === String(id));
  function send(action, item) {
    const box = item ? [item.x, item.y, item.w, item.h] : null;
    setTriggerValue("event", { action, id: item ? item.id : null, box, nonce: nonce() });
  }

  function render() {
    const [W, H] = size();
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    while (svg.firstChild) svg.removeChild(svg.firstChild);
    items.forEach((it) => {
      const sel = String(it.id) === String(selected);
      const cls = ["box", it.kind || "text", it.review ? "review" : "", it.hidden ? "hidden" : "",
                   sel ? "selected" : ""].join(" ");
      const g = el("g", { class: cls }, svg);
      const body = el("rect", { class: "body", x: it.x * W, y: it.y * H, width: it.w * W, height: it.h * H,
        tabindex: "0", role: "button", "data-box": it.id, "aria-label": it.title || it.label || "Bloc",
        "aria-pressed": sel ? "true" : "false" }, g);
      const title = el("title", {}, body); title.textContent = it.title || it.label || "";
      const tag = String(it.label || "");
      if (tag) {
        el("rect", { class: "tag", x: it.x * W, y: it.y * H, width: 8 + 7 * tag.length, height: 15 }, g);
        const t = el("text", { x: it.x * W + 4, y: it.y * H + 11 }, g); t.textContent = tag;
      }
      if (sel && !data.readonly) {
        [[0, 0], [1, 0], [1, 1], [0, 1]].forEach((c, k) => {
          el("rect", { class: "handle h" + k, "data-handle": k, x: (it.x + c[0] * it.w) * W - 6,
            y: (it.y + c[1] * it.h) * H - 6, width: 12, height: 12 }, g);
        });
      }
    });
    if (draft) el("rect", { class: "draft", x: draft.x * W, y: draft.y * H, width: draft.w * W,
                             height: draft.h * H }, svg);
  }

  function down(e) {
    const p = point(e);
    const handle = e.target.getAttribute("data-handle");
    const id = e.target.getAttribute("data-box");
    if (handle !== null && selected !== null && !drawMode && !data.readonly) {
      const it = find(selected);
      drag = { type: "resize", corner: Number(handle), item: it, before: { x: it.x, y: it.y, w: it.w, h: it.h } };
    } else if (id !== null && !drawMode) {
      const it = find(id);
      const wasSelected = String(selected) === String(id);
      selected = String(id);
      drag = { type: data.readonly ? "none" : "move", start: p, item: it,
               before: { x: it.x, y: it.y, w: it.w, h: it.h }, select: !wasSelected };
      render();
    } else if (drawMode) {
      drag = { type: "draw", start: p };
      draft = { x: p[0], y: p[1], w: 0, h: 0 };
    } else {
      return;
    }
    svg.setPointerCapture(e.pointerId);
    e.preventDefault();
  }

  function move(e) {
    if (!drag) return;
    const p = point(e), d = drag;
    if (d.type === "move") {
      const b = d.before;
      d.item.x = clamp(b.x + p[0] - d.start[0], 0, 1 - b.w);
      d.item.y = clamp(b.y + p[1] - d.start[1], 0, 1 - b.h);
    } else if (d.type === "resize") {
      const b = d.before;
      let x0 = b.x, y0 = b.y, x1 = b.x + b.w, y1 = b.y + b.h;
      if (d.corner === 0 || d.corner === 3) x0 = Math.min(p[0], x1 - MIN); else x1 = Math.max(p[0], x0 + MIN);
      if (d.corner === 0 || d.corner === 1) y0 = Math.min(p[1], y1 - MIN); else y1 = Math.max(p[1], y0 + MIN);
      d.item.x = clamp(x0, 0, 1); d.item.y = clamp(y0, 0, 1);
      d.item.w = clamp(x1 - d.item.x, MIN, 1 - d.item.x); d.item.h = clamp(y1 - d.item.y, MIN, 1 - d.item.y);
    } else if (d.type === "draw") {
      draft = { x: Math.min(p[0], d.start[0]), y: Math.min(p[1], d.start[1]),
                w: Math.abs(p[0] - d.start[0]), h: Math.abs(p[1] - d.start[1]) };
    } else return;
    render();
  }

  function up() {
    const d = drag; drag = null;
    if (!d) return;
    if (d.type === "draw") {
      const dr = draft; draft = null; render();
      if (dr && dr.w > 0.01 && dr.h > 0.005) {
        setDraw(false);
        setTriggerValue("event", { action: "create", id: null, box: [dr.x, dr.y, dr.w, dr.h], nonce: nonce() });
      }
      return;
    }
    const b = d.before, it = d.item;
    if (b.x !== it.x || b.y !== it.y || b.w !== it.w || b.h !== it.h) send("update", it);
    else if (d.select) send("select", it);
  }

  function key(e) {
    const id = e.target.getAttribute && e.target.getAttribute("data-box");
    if (id === null || id === undefined) return;
    const it = find(id);
    if (!it) return;
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); selected = String(id); render(); send("select", it); return; }
    if (data.readonly) return;
    const step = e.shiftKey ? 0.01 : 0.002;
    const m = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] }[e.key];
    if (!m) return;
    e.preventDefault();
    if (e.altKey) {
      it.w = clamp(it.w + m[0], MIN, 1 - it.x); it.h = clamp(it.h + m[1], MIN, 1 - it.y);
    } else {
      it.x = clamp(it.x + m[0], 0, 1 - it.w); it.y = clamp(it.y + m[1], 0, 1 - it.h);
    }
    selected = String(id);
    render();
    const node = svg.querySelector(`[data-box="${CSS.escape(String(id))}"]`);
    if (node) node.focus({ preventScroll: true });
    clearTimeout(keyTimer);
    keyTimer = setTimeout(() => send("update", it), 600);
  }

  svg.onpointerdown = down;
  svg.onpointermove = move;
  svg.onpointerup = up;
  svg.onpointercancel = up;
  svg.onkeydown = key;
  const onResize = () => render();
  window.addEventListener("resize", onResize);
  img.onload = render;
  if (img.complete) render();
  return () => {
    clearTimeout(keyTimer);
    window.removeEventListener("resize", onResize);
  };
}
"""

_BOX_EDITOR = st.components.v2.component("bookocr_box_editor", html=_HTML, css=_CSS, js=_JS)


def block_items(page: Page, labels: dict[str, str], low_confidence: float) -> list[dict[str, Any]]:
    """Blocs de la page pour l'éditeur : couleur selon le type, orange si à vérifier, rang de lecture."""
    items = []
    for rank, block in enumerate(page.blocks_in_order(), start=1):
        is_text = isinstance(block, TextBlock)
        review = is_text and (block.needs_review or (0 <= block.confidence < low_confidence
                                                     and not block.user_modified))
        excerpt = block.text[:60] if is_text else ""
        title = f"{rank}. {labels.get(block.id, '')}" + (" — à vérifier" if review else "")
        if excerpt:
            title += f" : {excerpt}"
        items.append({"id": block.id, "x": block.box.x, "y": block.box.y, "w": block.box.w, "h": block.box.h,
                      "kind": "text" if is_text else "image", "review": bool(review), "hidden": block.hidden,
                      "label": str(rank), "title": title})
    return items


def box_editor(image_uri: str, items: list[dict[str, Any]], *, key: str, selected: str | None = None,
               width: int = 700, allow_draw: bool = True, readonly: bool = False,
               draw_label: str = "Dessiner un bloc", alt: str = "Page corrigée") -> dict[str, Any] | None:
    """Affiche l'éditeur ; renvoie l'événement de l'utilisateur (une seule fois par `nonce`) ou None."""
    result = _BOX_EDITOR(key=key, data={"image": image_uri, "boxes": items, "selected": selected,
                                        "width": int(width), "allow_draw": allow_draw, "readonly": readonly,
                                        "draw_label": draw_label, "alt": alt},
                         on_event_change=lambda: None, height="content")
    event = getattr(result, "event", None)
    if not isinstance(event, dict) or not event.get("nonce"):
        return None
    seen = st.session_state.setdefault("_box_editor_nonces", [])
    if event["nonce"] in seen:
        return None
    seen.append(event["nonce"])
    del seen[:-100]
    return event


def event_box(event: dict[str, Any]) -> Box | None:
    """Boîte normalisée d'un événement (bornée à la page) ; None si absente ou invalide."""
    raw = event.get("box")
    if not isinstance(raw, list | tuple) or len(raw) != 4:
        return None
    try:
        x, y, w, h = (float(value) for value in raw)
    except (TypeError, ValueError):
        return None
    if w <= 0 or h <= 0:
        return None
    return Box.from_edges(x, y, x + w, y + h)


def is_image_block(block: TextBlock | ImageBlock) -> bool:
    return isinstance(block, ImageBlock)
