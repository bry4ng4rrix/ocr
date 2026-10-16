@AGENTS.md

# Spécifique à Claude Code

## Coordination (agent principal)

L'agent principal reste responsable du projet. Pour chaque fonctionnalité :

1. Lire `planning.md` et le code concerné ; définir les critères d'acceptation.
2. Si la fonctionnalité traverse plusieurs modules, **écrire d'abord le contrat** (signatures, modèles) dans `docs/ARCHITECTURE.md`, puis déléguer.
3. Déléguer au sous-agent propriétaire des fichiers (tableau ci-dessous). Un fichier n'a qu'un propriétaire à la fois : deux agents ne modifient jamais le même module en parallèle.
4. Paralléliser seulement des tâches indépendantes (fichiers disjoints) ; sinon séquentiel.
5. Intégrer, lancer les tests (`/run-tests`), corriger les conflits, mettre à jour `planning.md` et la doc, rendre compte.

| Sous-agent (`.claude/agents/`) | Fichiers dont il est propriétaire |
|---|---|
| `software-architect` | `docs/ARCHITECTURE.md`, `docs/TECHNICAL_DECISIONS.md`, `models/` |
| `ocr-image-specialist` | `services/orientation_service.py`, `services/image_service.py`, `services/ocr_service.py` |
| `document-layout-specialist` | `services/layout_service.py`, `services/composition_service.py`, `services/typography_service.py` |
| `docx-pdf-specialist` | `services/document_service.py`, `services/pdf_service.py`, `services/fonts.py` |
| `streamlit-ux-specialist` | `app.py`, `components/`, `.streamlit/` |
| `qa-test-engineer` | `tests/`, `docs/TEST_STRATEGY.md`, `docs/TEST_REPORT.md` |
| `integration-documentation` | `README.md`, `requirements.txt`, `config.py`, `services/project_service.py`, `services/processing_service.py`, `CHANGELOG.md`, `docs/KNOWN_LIMITATIONS.md`, `docs/USER_GUIDE.md` |

`roadmap.md` et `planning.md` sont tenus par l'agent principal. Chaque sous-agent peut lire tout le dépôt et ajouter des tests dans `tests/` pour son module ; la cohérence de `tests/` reste à `qa-test-engineer`.

## Skills et commandes du projet (`.claude/skills/`)

- Savoir-faire (chargés automatiquement selon le contexte) : `ocr-page-processing`, `layout-reconstruction`, `docx-export-validation`, `pdf-export-validation`, `python-testing`, `project-code-review`, et `developing-with-streamlit` (fourni par Streamlit).
- Commandes manuelles : `/project-status`, `/project-plan`, `/run-tests`, `/review-changes`, `/validate-exports`, `/debug-ocr`, `/update-docs`.

Le skill de revue s'appelle `project-code-review` et non `code-review` : un skill de projet de ce nom remplacerait la commande intégrée `/code-review` de Claude Code (voir TECHNICAL_DECISIONS, D-13).
