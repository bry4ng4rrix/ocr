# Reconstruction de livres imprimés

Application locale **Python + Streamlit** : photographies de pages → livre **DOCX modifiable** et **PDF**, sans envoyer les documents à un service externe (sauf activation explicite, désactivée par défaut).

## Installation

Dépendances système (Arch) :

```bash
sudo pacman -S tesseract tesseract-data-fra tesseract-data-eng tesseract-data-deu tesseract-data-osd
# facultatif : conversion DOCX → PDF
sudo pacman -S libreoffice-fresh
```

Environnement Python :

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Python 3.12+ visé ; **3.14** testé. PaddleOCR n’est **pas** installé : PaddlePaddle ne fournit pas de roue pour Python 3.14 (voir `docs/audit.md`).

## Lancement

```bash
.venv/bin/streamlit run app.py
```

Les projets vivent dans `data/projects/` (ignoré par Git). Les originaux ne sont jamais réécrits.

Copier [`.env.example`](.env.example) au besoin. Préfixe des réglages locaux : `BOOKOCR_*`.

## Tests

```bash
.venv/bin/python -m pytest -q
```

## Documentation

| Fichier | Contenu |
|---|---|
| [docs/audit.md](docs/audit.md) | Audit de l’existant et plan de migration |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Contrats des services et modèles |
| [docs/TECHNICAL_DECISIONS.md](docs/TECHNICAL_DECISIONS.md) | Décisions (OCR, persistance, modes DOCX) |
| [docs/pipeline.md](docs/pipeline.md) | Enchaînement des étapes |
| [AGENTS.md](AGENTS.md) | Règles de développement |
| [planning.md](planning.md) | Tâches et états |
| [roadmap.md](roadmap.md) | Phases |
