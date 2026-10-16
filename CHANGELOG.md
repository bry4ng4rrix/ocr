# Changelog

Format inspiré de *Keep a Changelog*. Dates au format AAAA-MM-JJ.

## [Non publié]

### Ajouté — 2026-10-16
- Organisation Claude Code : 7 sous-agents (`.claude/agents/`), 6 skills de savoir-faire et 7 commandes (`.claude/skills/`), dont 3 scripts de diagnostic (`ocr_probe.py`, `check_docx.py`, `check_pdf.py`).
- Règles du projet : `AGENTS.md` (source unique) importé par `CLAUDE.md`.
- Documentation : `docs/ARCHITECTURE.md` (contrats v1, diagramme Mermaid), `docs/TECHNICAL_DECISIONS.md`, `docs/TEST_STRATEGY.md`, `docs/KNOWN_LIMITATIONS.md`, `roadmap.md`, `planning.md`.

### Contexte
- L'application précédente (PDF seul, commit `ef66f25`) a été supprimée par l'utilisateur (commit `7760065`). Ses algorithmes servent de référence (TECHNICAL_DECISIONS D-01).
