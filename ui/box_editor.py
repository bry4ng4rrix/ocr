"""SVG box editor (Streamlit components v2) and the Python side that applies its events.

Frontend contract (Streamlit 1.66, `st.components.v2.component`): the JS module's default export
receives `{data, setTriggerValue, parentElement, ...}` on every render (and returns a cleanup
function). It sends one trigger per user action:

    setTriggerValue("event", {action: "select" | "update" | "create", id, box: [x, y, w, h], nonce})

Coordinates are normalized 0..1 in the page frame, whatever the zoom. Python mounts the component
with `on_event_change` (needed for the trigger to be registered) and reads `result.event`; each
event is applied once per nonce by `apply_event()` (history, change, save), which is kept free of
Streamlit so it can be tested directly.
"""
from __future__ import annotations

import logging
from typing import Any

import streamlit as st

from core.document_models import BLOCK_LABELS, Box, ImageBlock, Page, Project, TextBlock

logger = logging.getLogger("livreocr.ui")

_HTML = '<div class="be-root"></div>'

_CSS = """
.be-root { font-family: var(--st-font, sans-serif); font-size: 13px; color: var(--st-text-color, #222); }
.be-bar { display: flex; gap: 6px; align-items: center; margin-bottom: 6px; flex-wrap: wrap; }
.be-bar button { border: 1px solid #b8c2cc; background: #fff; border-radius: 6px; padding: 3px 10px; cursor: pointer; }
.be-bar button.on { background: #1f6f8b; color: #fff; border-color: #1f6f8b; }
.be-bar .hint { color: #667; }
.be-stage { position: relative; display: inline-block; line-height: 0; user-select: none; }
.be-stage img { display: block; max-width: none; }
.be-stage svg { position: absolute; left: 0; top: 0; width: 100%; height: 100%; touch-action: none; }
.be-stage svg.drawing { cursor: crosshair; }
.box rect.body { fill: rgba(31,111,139,0.06); stroke-width: 2; cursor: move; }
.box.text rect.body { stroke: #1f6f8b; }
.box.image rect.body { stroke: #2e8b57; fill: rgba(46,139,87,0.06); }
.box.local rect.body { stroke: #8b3fa8; fill: rgba(139,63,168,0.08); stroke-dasharray: 6 3; }
.box.hidden rect.body { stroke: #999; stroke-dasharray: 3 3; }
.box.review rect.body { stroke: #e67e00; stroke-width: 3; }
.box.selected rect.body { stroke: #d62828; stroke-width: 3; fill: rgba(214,40,40,0.08); }
.box rect.body:focus { outline: none; stroke: #d62828; }
.box rect.tag { fill: #1f6f8b; }
.box.image rect.tag { fill: #2e8b57; }
.box.local rect.tag { fill: #8b3fa8; }
.box.review rect.tag { fill: #e67e00; }
.box.selected rect.tag { fill: #d62828; }
.box text { fill: #fff; font-size: 11px; font-family: sans-serif; pointer-events: none; }
rect.handle { fill: #fff; stroke: #d62828; stroke-width: 2; cursor: nwse-resize; }
rect.handle.h1, rect.handle.h3 { cursor: nesw-resize; }
rect.draft { fill: rgba(214,40,40,0.1); stroke: #d62828; stroke-dasharray: 4 3; stroke-width: 2; }
rect.frame { fill: none; stroke: #bbb; stroke-dasharray: 2 4; }
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
  root.innerHTML = "";
  const items = (data.boxes || []).map((b) => ({ ...b }));
  // frame: where the page frame sits in the displayed image (fractions); 0,0,1,1 by default.
  const F = data.frame || [0, 0, 1, 1];
  let selected = data.selected || null;
  let drawMode = false;
  let drag = null, draft = null, keyTimer = null, keyBefore = null;

  const bar = document.createElement("div");
  bar.className = "be-bar";
  const bSel = document.createElement("button"); bSel.textContent = "Sélectionner / déplacer";
  const bDraw = document.createElement("button"); bDraw.textContent = data.draw_label || "Dessiner une zone";
  const hint = document.createElement("span"); hint.className = "hint";
  hint.textContent = "Clic : sélection · glisser : déplacer · coin : redimensionner · flèches : déplacer (Maj : plus vite, Alt : taille)";
  if (data.allow_draw !== false) { bar.append(bSel, bDraw); }
  bar.append(hint);
  root.appendChild(bar);

  const stage = document.createElement("div"); stage.className = "be-stage";
  const img = document.createElement("img");
  img.src = data.image; img.alt = data.alt || "Page";
  img.style.width = (data.width || 800) + "px";
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
  // image fraction <-> frame fraction
  const toFrame = (px, py) => [clamp((px - F[0]) / F[2], 0, 1), clamp((py - F[1]) / F[3], 0, 1)];
  function point(e) {
    const r = img.getBoundingClientRect();
    return toFrame((e.clientX - r.left) / r.width, (e.clientY - r.top) / r.height);
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
    const X = (v) => (F[0] + v * F[2]) * W, Y = (v) => (F[1] + v * F[3]) * H;
    const SW = (v) => v * F[2] * W, SH = (v) => v * F[3] * H;
    if (F[0] > 0 || F[1] > 0 || F[2] < 1 || F[3] < 1)
      el("rect", { class: "frame", x: X(0), y: Y(0), width: SW(1), height: SH(1) }, svg);
    items.forEach((it) => {
      const sel = String(it.id) === String(selected);
      const cls = ["box", it.kind || "text", it.review ? "review" : "", it.hidden ? "hidden" : "", sel ? "selected" : ""].join(" ");
      const g = el("g", { class: cls }, svg);
      const body = el("rect", { class: "body", x: X(it.x), y: Y(it.y), width: SW(it.w), height: SH(it.h),
        tabindex: "0", role: "button", "data-box": it.id, "aria-label": it.title || it.label || "Bloc",
        "aria-pressed": sel ? "true" : "false" }, g);
      const title = el("title", {}, body); title.textContent = it.title || it.label || "";
      const tag = String(it.label || "");
      if (tag) {
        el("rect", { class: "tag", x: X(it.x), y: Y(it.y), width: 8 + 7 * tag.length, height: 15 }, g);
        const t = el("text", { x: X(it.x) + 4, y: Y(it.y) + 11 }, g); t.textContent = tag;
      }
      if (sel && !it.locked) {
        [[0, 0], [1, 0], [1, 1], [0, 1]].forEach((c, k) => {
          el("rect", { class: "handle h" + k, "data-handle": k, x: X(it.x + c[0] * it.w) - 6,
            y: Y(it.y + c[1] * it.h) - 6, width: 12, height: 12 }, g);
        });
      }
    });
    if (draft) el("rect", { class: "draft", x: X(draft.x), y: Y(draft.y), width: SW(draft.w), height: SH(draft.h) }, svg);
  }

  function select(id) {
    selected = id === null || id === undefined ? null : String(id);
    render();
    if (selected !== null) send("select", find(selected));
  }

  function down(e) {
    const p = point(e);
    const handle = e.target.getAttribute("data-handle");
    const id = e.target.getAttribute("data-box");
    if (handle !== null && selected !== null && !drawMode) {
      const it = find(selected);
      drag = { type: "resize", corner: Number(handle), item: it, before: { x: it.x, y: it.y, w: it.w, h: it.h } };
    } else if (id !== null && !drawMode) {
      const it = find(id);
      const wasSelected = String(selected) === String(id);
      selected = String(id);
      drag = { type: "move", start: p, item: it, before: { x: it.x, y: it.y, w: it.w, h: it.h }, select: !wasSelected };
      if (it.locked) drag.type = "none";
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
    if (!it || it.locked) return;
    const step = e.shiftKey ? 0.01 : 0.002;
    const m = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] }[e.key];
    if (!m) { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); select(id); } return; }
    e.preventDefault();
    if (!keyBefore) keyBefore = { x: it.x, y: it.y, w: it.w, h: it.h };
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
    keyTimer = setTimeout(() => { keyBefore = null; send("update", it); }, 600);
  }

  svg.addEventListener("pointerdown", down);
  svg.addEventListener("pointermove", move);
  svg.addEventListener("pointerup", up);
  svg.addEventListener("pointercancel", up);
  svg.addEventListener("keydown", key);
  const onResize = () => render();
  window.addEventListener("resize", onResize);
  img.addEventListener("load", render);
  if (img.complete) render();
  return () => {
    clearTimeout(keyTimer);
    window.removeEventListener("resize", onResize);
  };
}
"""

_component = None


def _renderer():
    global _component
    if _component is None:
        _component = st.components.v2.component("livreocr_box_editor", html=_HTML, css=_CSS, js=_JS)
    return _component


def block_items(page: Page, selected: str | None = None) -> list[dict]:
    """The page's real blocks as editor items (colour by kind, orange for needs_review, order label)."""
    items = []
    for block in page.ordered_blocks():
        kind = "image" if isinstance(block, ImageBlock) else "text"
        excerpt = (block.text[:60] if isinstance(block, TextBlock) else block.detection) or ""
        title = f"{block.reading_order + 1}. {BLOCK_LABELS.get(block.type, block.type.value)}"
        if block.needs_review:
            title += " — à vérifier" + (f" ({block.review_reason})" if block.review_reason else "")
        if excerpt:
            title += f" : {excerpt}"
        items.append({"id": block.id, "x": block.x, "y": block.y, "w": block.width, "h": block.height,
                      "kind": kind, "review": bool(block.needs_review), "hidden": bool(block.hidden),
                      "label": str(block.reading_order + 1), "title": title})
    return items


def local_items(page: Page) -> list[dict]:
    return [{"id": loc.id, "x": loc.box.x, "y": loc.box.y, "w": loc.box.width, "h": loc.box.height,
             "kind": "local", "label": str(i + 1), "title": loc.label or f"Zone {i + 1}",
             "hidden": not loc.enabled} for i, loc in enumerate(page.local_adjustments)]


def box_editor(image, items: list[dict], key: str, selected: str | None = None, width: int = 800,
               frame: tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0),
               draw_label: str = "Dessiner une zone", allow_draw: bool = True) -> dict | None:
    """Mount the editor; returns the user's event (dict) or None. Never raises: on failure a
    message is shown and the numeric fallback below the editor remains available."""
    from . import state
    try:
        data = {"image": state.to_jpeg_data_uri(image), "boxes": items, "selected": selected, "width": int(width),
                "frame": [float(v) for v in frame], "draw_label": draw_label, "allow_draw": allow_draw}
        result = _renderer()(key=key, data=data, on_event_change=lambda: None, height="content")
        event = getattr(result, "event", None)
        return event if isinstance(event, dict) else None
    except Exception as exc:
        logger.exception("éditeur de boîtes indisponible")
        st.info(f"Éditeur graphique indisponible ({exc.__class__.__name__}) : utilisez la sélection et les "
                "champs numériques ci-dessous.")
        st.image(image, width=width)
        return None


def is_new(event: dict | None, seen: list) -> bool:
    """True the first time a nonce is seen (events are applied once)."""
    if not event or not event.get("nonce"):
        return False
    if event["nonce"] in seen:
        return False
    seen.append(event["nonce"])
    del seen[:-200]
    return True


def _box(event: dict) -> Box | None:
    raw = event.get("box")
    if not raw or len(raw) != 4:
        return None
    x, y, w, h = (float(v) for v in raw)
    return Box.clamped(x, y, w, h)


def apply_event(project: Project, page: Page, event: dict, history, target: str = "blocks",
                create_kind: str = "text") -> dict[str, Any]:
    """Apply one editor event to `page` (blocks, or local corrections when target="locals").

    "select" changes nothing in the project; "update" moves / resizes the item (history, save);
    "create" adds a text block (or a local correction zone, or an illustration cut from the frame
    when create_kind="image"). Returns {"selected": id or None, "changed": bool, "warnings": [...]}.
    """
    from services import project_service, storage_service

    action = event.get("action")
    item_id = event.get("id")
    out: dict[str, Any] = {"selected": item_id, "changed": False, "warnings": []}
    box = _box(event)
    if action == "select":
        return out
    if action == "update" and box is not None and item_id:
        if target == "locals":
            local = next((loc for loc in page.local_adjustments if loc.id == item_id), None)
            if local is None:
                return out
            history.record(page, "Zone de correction déplacée")
            local.box = box
        else:
            block = page.block(item_id)
            if block is None:
                return out
            history.record(page, "Bloc déplacé ou redimensionné")
            out["warnings"] = project_service.set_block_box(block, box.x, box.y, box.width, box.height)
        out["changed"] = True
    elif action == "create" and box is not None:
        if target == "locals":
            history.record(page, "Zone de correction ajoutée")
            local = project_service.add_local_adjustment(page, box)
            out["selected"] = local.id
        elif create_kind == "image":
            from core import illustration_extraction, image_loader
            from services import processing_service
            frame = processing_service.ensure_frame(project, page)
            crop = illustration_extraction.crop_region(frame, box)
            history.record(page, "Illustration ajoutée")
            block = project_service.add_image_block(project, page, image_loader.to_png_bytes(crop),
                                                    "zone.png", box)
            block.source_box = box
            block.detection = "manual"
            out["selected"] = block.id
        else:
            history.record(page, "Bloc texte ajouté")
            block = project_service.add_text_block(page, box)
            out["selected"] = block.id
        out["changed"] = True
    if out["changed"]:
        storage_service.save_project(project)
    return out
