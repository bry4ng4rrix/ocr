"""Sélecteur de recadrage (Streamlit Custom Components v2) : rectangle tracé à la souris ou 4 coins libres.

Contrat du composant : le module JS reçoit `data` (image, coins initiaux, mode) et n'envoie qu'un déclencheur,
au clic sur « Appliquer » :

    setTriggerValue("crop", {corners: [[x, y] × 4 (haut-gauche, haut-droit, bas-droit, bas-gauche)], nonce})

Les coordonnées sont normalisées 0..1 sur la photographie originale affichée (après EXIF). Le composant
n'enregistre rien : l'écran passe les coins à `scanning_service.set_manual_quad`, qui les valide
géométriquement (convexe, assez grand, angles plausibles). Repli accessible : les champs numériques.
"""
from __future__ import annotations

from typing import Any

import streamlit as st

from models.geometry import Point

_HTML = """<div class="cs-root"></div>"""

_CSS = """
.cs-root { font-family: var(--st-font, sans-serif); font-size: 13px; color: var(--st-text-color, #222); }
.cs-bar { display: flex; gap: 6px; align-items: center; margin-bottom: 6px; flex-wrap: wrap; }
.cs-bar button { border: 1px solid #b8c2cc; background: #fff; border-radius: 6px; padding: 4px 10px;
  cursor: pointer; font: inherit; }
.cs-bar button.on { background: var(--st-primary-color, #1f6f8b); color: #fff; border-color: var(--st-primary-color, #1f6f8b); }
.cs-bar button.apply { background: var(--st-primary-color, #1f6f8b); color: #fff; border-color: var(--st-primary-color, #1f6f8b); }
.cs-bar .hint { color: #667; }
.cs-stage { position: relative; display: inline-block; line-height: 0; user-select: none; max-width: 100%; }
.cs-stage img { display: block; max-width: 100%; height: auto; }
.cs-stage svg { position: absolute; left: 0; top: 0; width: 100%; height: 100%; touch-action: none; cursor: crosshair; }
.shade { fill: rgba(0, 0, 0, 0.45); pointer-events: none; }
.outline { fill: rgba(255, 255, 255, 0.01); stroke: #ffd60a; stroke-width: 2; cursor: move; }
.grid { stroke: rgba(255, 214, 10, 0.55); stroke-width: 1; stroke-dasharray: 4 4; pointer-events: none; }
.corner { fill: #ffd60a; stroke: #1b1b1b; stroke-width: 1.5; cursor: grab; }
.corner:focus { outline: none; stroke: #d62828; stroke-width: 3; }
.cs-info { margin-top: 4px; color: #556; line-height: 1.4; }
"""

_JS = r"""
const NS = "http://www.w3.org/2000/svg";
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
function el(name, attrs, parent) {
  const node = document.createElementNS(NS, name);
  for (const k in attrs) node.setAttribute(k, attrs[k]);
  if (parent) parent.appendChild(node);
  return node;
}
function nonce() { return Date.now().toString(36) + Math.random().toString(36).slice(2, 8); }
const FULL = [[0, 0], [1, 0], [1, 1], [0, 1]];

export default function (component) {
  const { data, setTriggerValue, parentElement } = component;
  const root = parentElement.querySelector(".cs-root");
  if (!root || !data) return;
  root.replaceChildren();
  const initial = (data.corners && data.corners.length === 4) ? data.corners.map((c) => [c[0], c[1]]) : FULL.map((c) => [...c]);
  let corners = initial.map((c) => [...c]);
  let mode = data.mode === "quad" ? "quad" : "rect";
  let drag = null, focused = 0;

  const bar = document.createElement("div"); bar.className = "cs-bar";
  const button = (label, cls) => { const b = document.createElement("button"); b.type = "button"; b.textContent = label;
    if (cls) b.className = cls; bar.appendChild(b); return b; };
  const bRect = button("Rectangle");
  const bQuad = button("4 coins (perspective)");
  const bFull = button("Image entière");
  const bReset = button("Contour proposé");
  const bApply = button("Appliquer le recadrage", "apply");
  const hint = document.createElement("span"); hint.className = "hint"; bar.appendChild(hint);
  root.appendChild(bar);

  const stage = document.createElement("div"); stage.className = "cs-stage";
  const img = document.createElement("img"); img.src = data.image; img.alt = data.alt || "Photographie originale";
  img.style.width = (data.width || 900) + "px";
  const svg = el("svg", { role: "group", "aria-label": "Zone de recadrage" });
  stage.append(img, svg); root.appendChild(stage);
  const info = document.createElement("div"); info.className = "cs-info"; root.appendChild(info);

  function asRect() {
    const xs = corners.map((c) => c[0]), ys = corners.map((c) => c[1]);
    const x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = Math.min(...ys), y1 = Math.max(...ys);
    corners = [[x0, y0], [x1, y0], [x1, y1], [x0, y1]];
  }
  function setMode(next) {
    mode = next;
    if (mode === "rect") asRect();
    bRect.classList.toggle("on", mode === "rect"); bQuad.classList.toggle("on", mode === "quad");
    hint.textContent = mode === "rect"
      ? "Glisser sur la photo : nouvelle sélection · glisser dedans : déplacer · coins : redimensionner"
      : "Glisser chaque coin sur un coin de la page · flèches : ajuster le coin choisi (Maj : plus vite)";
    render();
  }
  const size = () => { const r = img.getBoundingClientRect(); return [r.width || 1, r.height || 1]; };
  function point(e) {
    const r = img.getBoundingClientRect();
    return [clamp((e.clientX - r.left) / r.width, 0, 1), clamp((e.clientY - r.top) / r.height, 0, 1)];
  }

  function render() {
    const [W, H] = size();
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    while (svg.firstChild) svg.removeChild(svg.firstChild);
    const pts = corners.map((c) => `${c[0] * W},${c[1] * H}`).join(" ");
    el("path", { class: "shade", "fill-rule": "evenodd",
      d: `M0,0 H${W} V${H} H0 Z M${corners.map((c) => `${c[0] * W},${c[1] * H}`).join(" L")} Z` }, svg);
    el("polygon", { class: "outline", points: pts, "data-body": "1" }, svg);
    for (const t of [1 / 3, 2 / 3]) {                       // grille des tiers (aide à l'alignement)
      const a = [corners[0][0] + t * (corners[1][0] - corners[0][0]), corners[0][1] + t * (corners[1][1] - corners[0][1])];
      const b = [corners[3][0] + t * (corners[2][0] - corners[3][0]), corners[3][1] + t * (corners[2][1] - corners[3][1])];
      el("line", { class: "grid", x1: a[0] * W, y1: a[1] * H, x2: b[0] * W, y2: b[1] * H }, svg);
      const c = [corners[0][0] + t * (corners[3][0] - corners[0][0]), corners[0][1] + t * (corners[3][1] - corners[0][1])];
      const d = [corners[1][0] + t * (corners[2][0] - corners[1][0]), corners[1][1] + t * (corners[2][1] - corners[1][1])];
      el("line", { class: "grid", x1: c[0] * W, y1: c[1] * H, x2: d[0] * W, y2: d[1] * H }, svg);
    }
    const names = ["haut-gauche", "haut-droit", "bas-droit", "bas-gauche"];
    corners.forEach((c, k) => {
      const handle = el("circle", { class: "corner", cx: c[0] * W, cy: c[1] * H, r: 8, "data-corner": k,
        tabindex: "0", role: "button", "aria-label": `Coin ${names[k]}` }, svg);
      handle.onfocus = () => { focused = k; };
    });
    const w = Math.round(100 * Math.max(corners[1][0] - corners[0][0], corners[2][0] - corners[3][0]));
    const h = Math.round(100 * Math.max(corners[3][1] - corners[0][1], corners[2][1] - corners[1][1]));
    info.textContent = `Sélection : environ ${w} % × ${h} % de la photo. Les bords sont vérifiés à l'application.`;
  }

  function down(e) {
    const p = point(e);
    const k = e.target.getAttribute("data-corner");
    if (k !== null) {
      drag = { type: "corner", corner: Number(k) };
    } else if (e.target.getAttribute("data-body")) {
      drag = { type: "move", start: p, before: corners.map((c) => [...c]) };
    } else if (mode === "rect") {
      drag = { type: "draw", start: p };
      corners = [[p[0], p[1]], [p[0], p[1]], [p[0], p[1]], [p[0], p[1]]];
    } else {
      return;
    }
    svg.setPointerCapture(e.pointerId);
    e.preventDefault();
  }
  function move(e) {
    if (!drag) return;
    const p = point(e);
    if (drag.type === "corner") {
      const k = drag.corner;
      if (mode === "rect") {                               // le coin opposé reste fixe
        const o = corners[(k + 2) % 4];
        const x0 = Math.min(p[0], o[0]), x1 = Math.max(p[0], o[0]), y0 = Math.min(p[1], o[1]), y1 = Math.max(p[1], o[1]);
        corners = [[x0, y0], [x1, y0], [x1, y1], [x0, y1]];
        drag.corner = p[0] <= o[0] ? (p[1] <= o[1] ? 0 : 3) : (p[1] <= o[1] ? 1 : 2);
      } else {
        corners[k] = p;
      }
    } else if (drag.type === "move") {
      const b = drag.before;
      const dx = clamp(p[0] - drag.start[0], -Math.min(...b.map((c) => c[0])), 1 - Math.max(...b.map((c) => c[0])));
      const dy = clamp(p[1] - drag.start[1], -Math.min(...b.map((c) => c[1])), 1 - Math.max(...b.map((c) => c[1])));
      corners = b.map((c) => [c[0] + dx, c[1] + dy]);
    } else if (drag.type === "draw") {
      const s = drag.start;
      const x0 = Math.min(p[0], s[0]), x1 = Math.max(p[0], s[0]), y0 = Math.min(p[1], s[1]), y1 = Math.max(p[1], s[1]);
      corners = [[x0, y0], [x1, y0], [x1, y1], [x0, y1]];
    }
    render();
  }
  function up() { drag = null; }
  function key(e) {
    const k = e.target.getAttribute && e.target.getAttribute("data-corner");
    if (k === null || k === undefined) return;
    const step = e.shiftKey ? 0.01 : 0.002;
    const m = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] }[e.key];
    if (!m) return;
    e.preventDefault();
    const i = Number(k);
    const x = clamp(corners[i][0] + m[0], 0, 1), y = clamp(corners[i][1] + m[1], 0, 1);
    if (mode === "rect") {                                 // le rectangle suit : coins voisins mis à jour
      const sameX = { 0: 3, 1: 2, 2: 1, 3: 0 }[i], sameY = { 0: 1, 1: 0, 2: 3, 3: 2 }[i];
      corners[sameX][0] = x; corners[sameY][1] = y;
    }
    corners[i] = [x, y];
    render();
    const node = svg.querySelector(`[data-corner="${i}"]`);
    if (node) node.focus({ preventScroll: true });
  }

  bRect.onclick = () => setMode("rect");
  bQuad.onclick = () => setMode("quad");
  bFull.onclick = () => { corners = FULL.map((c) => [...c]); render(); };
  bReset.onclick = () => { corners = initial.map((c) => [...c]); if (mode === "rect") asRect(); render(); };
  bApply.onclick = () => setTriggerValue("crop", { corners: corners.map((c) => [Number(c[0].toFixed(5)), Number(c[1].toFixed(5))]),
                                                   mode, nonce: nonce() });
  svg.onpointerdown = down; svg.onpointermove = move; svg.onpointerup = up; svg.onpointercancel = up;
  svg.onkeydown = key;
  const onResize = () => render();
  window.addEventListener("resize", onResize);
  img.onload = () => setMode(mode);
  if (img.complete) setMode(mode);
  return () => window.removeEventListener("resize", onResize);
}
"""

_CROP_SELECTOR = st.components.v2.component("bookocr_crop_selector", html=_HTML, css=_CSS, js=_JS)


def crop_selector(image_uri: str, corners: list[Point] | None, *, key: str, width: int = 900,
                  mode: str = "rect", alt: str = "Photographie originale") -> list[Point] | None:
    """Affiche le sélecteur ; renvoie les 4 coins choisis (une seule fois par clic sur « Appliquer ») ou None."""
    data: dict[str, Any] = {"image": image_uri, "width": int(width), "mode": mode, "alt": alt,
                            "corners": [[p.x, p.y] for p in corners] if corners else None}
    result = _CROP_SELECTOR(key=key, data=data, on_crop_change=lambda: None, height="content")
    event = getattr(result, "crop", None)
    if not isinstance(event, dict) or not event.get("nonce"):
        return None
    seen = st.session_state.setdefault("_crop_selector_nonces", [])
    if event["nonce"] in seen:
        return None
    seen.append(event["nonce"])
    del seen[:-100]
    return corners_from_event(event)


def corners_from_event(event: dict[str, Any]) -> list[Point] | None:
    """Coins d'un événement, bornés à 0..1 ; None si le format est invalide."""
    raw = event.get("corners")
    if not isinstance(raw, list | tuple) or len(raw) != 4:
        return None
    try:
        return [Point(x=min(1.0, max(0.0, float(x))), y=min(1.0, max(0.0, float(y)))) for x, y in raw]
    except (TypeError, ValueError):
        return None
