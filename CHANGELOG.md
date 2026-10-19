# Changelog

Format inspiré de *Keep a Changelog*. Dates au format AAAA-MM-JJ.

## [Non publié]

### Ajouté — 2026-10-19 (numérisation intelligente)
- Écran « Numérisation intelligente » (`components/screens/smart_scan.py`). Il réunit :
  - l'état réel d'Ollama et de Tesseract, la progression par livre et pour toute la bibliothèque ;
  - la sélection des pages (toutes, à vérifier, en erreur, en attente) ;
  - l'aperçu original / redressée / améliorée, la comparaison avant/après et le zoom 100 % ;
  - les commandes : traiter, analyser avec Gemma, détecter les bords, améliorer, restaurer, valider ;
  - la rotation et le recadrage manuels, des curseurs avec aperçu local (sans Gemma) et l'export du PDF du livre.
- PDF fidèle recherchable (`pdf_export.build_searchable_pdf`, `validate_searchable_pdf`) :
  - photographie finale insérée sans recompression ;
  - mots OCR en mode de rendu 3, alignés sur leur boîte ;
  - rapport de validation JSON (pages, texte, invisibilité, mots témoins, octets d'image, couleurs).
- Analyse visuelle Gemma 3 facultative (`image_analysis_service`, `ollama_service.chat_json`) :
  - schéma JSON imposé et validation Pydantic avec liste blanche d'actions ;
  - cache par empreinte et modèle, nouvelles tentatives bornées, une requête à la fois ;
  - aucun téléchargement de modèle.
- Mesures et contrôle avant/après (`image_quality_service`) ; plan de correction déterministe confirmé par les mesures, exposition, rognage des bandes de fond par bord (`image_enhancement`).
- Orchestration (`scanning_service`), file persistante SQLite `scan_tasks` (`repositories/scan_repository`), worker de reprise (`workers/scan_runner`).
- Configuration `OLLAMA_MAX_RETRIES`, `OLLAMA_ANALYSIS_ENABLED`, `OLLAMA_AUTO_APPLY`, `OLLAMA_MAX_PARALLEL_REQUESTS`, `BOOKOCR_SCAN_*` ; lecture d'un fichier `.env` local (ignoré par Git, désactivée pendant les tests).
- Documentation : `docs/gemma3-scanning.md`, ARCHITECTURE §8, quality-control, audit §9. Tests : `test_ollama_analysis`, `test_scan_quality`, `test_searchable_pdf`, `test_scanning_service`, `test_ollama_integration` (facultatif).

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
