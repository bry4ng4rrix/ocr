# Changelog

Format inspiré de *Keep a Changelog*. Dates au format AAAA-MM-JJ.

## [Non publié]

### Ajouté — 2026-10-19
- Écran « Numérisation et amélioration PDF » : import d'images/PDF, recadrage et séparation réglable, modes d'amélioration locaux, validation et réordonnancement par page, export PDF/ZIP ; analyse Gemma facultative via Ollama.
- Écran « Aperçu en direct » (`components/screens/live_preview.py`) : comparaison côte à côte temps réel avec à gauche l'image (originale ou corrigée avec surlignage optionnel des boîtes) et à droite le texte extrait sur une page blanche sans fond, avec sélecteur de mode (disposition positionnée fidèle ou flux de lecture structuré).
- Module de rendu pur `components/live_preview_render.py` (`render_positioned_html`, `render_flow_html`, `draw_overlay_boxes`) et tests associés (`tests/test_live_preview.py`).

### Corrigé — 2026-10-19
- Les pages sans bande de routage sont normalisées avant l'écriture JSON ; un aller-retour de sauvegarde conserve désormais l'égalité entre l'objet projet et le projet rouvert.

### Ajouté — 2026-10-16
- Organisation Claude Code : 7 sous-agents (`.claude/agents/`), 6 skills de savoir-faire et 7 commandes (`.claude/skills/`), dont 3 scripts de diagnostic (`ocr_probe.py`, `check_docx.py`, `check_pdf.py`).
- Règles du projet : `AGENTS.md` (source unique) importé par `CLAUDE.md`.
- Documentation : `docs/ARCHITECTURE.md` (contrats v1, diagramme Mermaid), `docs/TECHNICAL_DECISIONS.md`, `docs/TEST_STRATEGY.md`, `docs/KNOWN_LIMITATIONS.md`, `roadmap.md`, `planning.md`.

### Contexte
- L'application précédente (PDF seul, commit `ef66f25`) a été supprimée par l'utilisateur (commit `7760065`). Ses algorithmes servent de référence (TECHNICAL_DECISIONS D-01).
