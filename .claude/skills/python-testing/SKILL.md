---
name: python-testing
description: Écrire, lancer et diagnostiquer les tests pytest de ce projet (pages synthétiques avec vérité terrain, tests conditionnés à Tesseract ou LibreOffice, AppTest Streamlit, relecture des DOCX/PDF générés) et prévenir les régressions. À utiliser dès qu'on ajoute une fonctionnalité, corrige un bogue ou qu'un test échoue.
---

# Tests Python du projet

## Commandes

```bash
.venv/bin/python -m pytest -q                         # toute la suite
.venv/bin/python -m pytest -q tests/test_ocr_service.py -k rotation
.venv/bin/python -m pytest -q -m "not slow"           # boucle rapide
.venv/bin/python -m pytest -q -x --lf                 # rejouer les derniers échecs
.venv/bin/python -m pytest -q -rs                     # afficher pourquoi des tests sont ignorés
```

Marqueurs (déclarés dans `pyproject.toml`) : `slow` (OCR réel, pages complètes), `tesseract`, `libreoffice`, `real_images` (photos réelles hors dépôt), `ui`.

## Choisir les tests à lancer

| Fichier modifié | Tests minimum |
|---|---|
| `models/` | `test_models.py`, puis toute la suite (contrats) |
| `services/orientation_service.py`, `image_service.py` | `test_orientation.py`, `test_image_service.py` |
| `services/ocr_service.py` | `test_ocr_service.py` |
| `services/layout_service.py`, `composition_service.py`, `typography_service.py` | `test_layout_service.py`, `test_composition.py`, `test_typography.py` |
| `services/document_service.py`, `pdf_service.py` | `test_document_service.py`, `test_pdf_service.py` |
| `services/project_service.py`, `processing_service.py` | `test_project_service.py`, `test_processing_service.py`, `test_integration.py` |
| `app.py`, `components/` | `test_ui.py` |

Avant de déclarer une tâche finie : toute la suite.

## Écrire un test utile

- Données : pages synthétiques de `tests/fixtures/synthetic.py` (texte connu, boîtes connues, images connues) ; vérité terrain comparée avec tolérance explicite.
- Fichiers : toujours `tmp_path` ; jamais d'écriture dans `data/` ni dans le dépôt. Un test sur photos réelles copie les fichiers dans `tmp_path` et vérifie que l'empreinte SHA-256 de l'original n'a pas changé.
- Dépendance absente : `pytest.importorskip` ou `pytest.mark.skipif(shutil.which("soffice") is None, reason="LibreOffice absent")`. Jamais un `assert True` de complaisance.
- Exports : relire le fichier produit (python-docx, PyMuPDF) et vérifier contenu, images et pages ; la seule existence du fichier ne prouve rien.
- Streamlit : `AppTest.from_file("app.py")`, injecter le projet par `session_state`, puis relire le projet **depuis le disque** pour vérifier l'effet.

## Diagnostiquer un échec

1. Relancer seul avec `-x -vv --tb=long`.
2. Classer : défaut du code, test erroné, limite de bibliothèque, environnement (outil ou langue absents).
3. Défaut du code → corriger le code (par son propriétaire), pas le test. Limite de bibliothèque → `xfail(reason=...)` + entrée dans docs/KNOWN_LIMITATIONS.md.
4. Ajouter un test de non-régression reproduisant le bogue avant de le corriger.
