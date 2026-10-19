# Agents et environnement

Le dépôt est déjà outillé pour **Claude Code** (`.claude/agents/`, skills). Dans **Cursor**, les mêmes rôles existent comme sous-agents (noms du projet). On **n’invente pas** une seconde arborescence `domain/` / `repositories/` ni des agents incompatibles.

## Correspondance prompt maître → rôles existants

| Rôle demandé | Rôle réel (Cursor / `.claude/agents`) | Fichiers |
|---|---|---|
| project-auditor | agent principal + `software-architect` | `docs/audit.md`, `docs/ARCHITECTURE.md` |
| document-ai-specialist | `ocr-image-specialist` (Tesseract ; Paddle plus tard) | `services/ocr_service.py` |
| image-processing-specialist | `ocr-image-specialist` | `orientation_service`, `image_service` |
| ai-fallback-specialist | à ajouter plus tard si Ollama/Gemini sont activés | `ollama_service`, `gemini_service` |
| pipeline-orchestrator | `integration-documentation` | `processing_service`, `project_service` |
| document-reconstruction-specialist | `docx-pdf-specialist` + `document-layout-specialist` | `document_service`, `pdf_service`, `composition_service` |
| quality-evaluation-specialist | `qa-test-engineer` | `quality_service`, `tests/` |
| streamlit-ui-specialist | `streamlit-ux-specialist` | `app.py`, `components/` |
| integration-test-specialist | `qa-test-engineer` | `tests/` |

Règles : un fichier, un propriétaire à la fois ; contrats dans `docs/ARCHITECTURE.md` avant un changement transversal ; pas de téléchargement de modèle sans autorisation.
