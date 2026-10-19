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

Copier [`.env.example`](.env.example) en `.env` au besoin (lu au démarrage). Préfixe des réglages locaux : `BOOKOCR_*`.

## Numérisation intelligente (PDF fidèle recherchable)

Écran **Traitement › Numérisation intelligente** : un PDF par livre. Chaque page montre la photographie redressée, recadrée et corrigée avec prudence, en couleurs, avec une couche OCR **invisible** et recherchable alignée mot par mot.

1. Tableau de bord : créer le livre (langues OCR, par exemple `fra` et `deu`) ; écran Pages : importer les photos dans l'ordre.
2. Numérisation intelligente : « Traiter les pages en attente ». Le traitement tourne en arrière-plan, sans bloquer l'interface, une page à la fois. Il reprend après une interruption et relance les erreurs sur demande.
3. Vérifier les pages « À vérifier » : rotation, recadrage manuel, réglages avec aperçu, puis « Valider la page ».
4. « Exporter le PDF du livre » : le PDF et son rapport de validation (`.validation.json`) sont écrits dans `data/projects/<id>/exports/`.

**Sans IA**, c'est le fonctionnement par défaut : OpenCV et Tesseract font tout le travail.

**Avec Gemma 3 (Ollama local)** : le serveur et le modèle doivent déjà être installés, l'application ne télécharge rien.

```bash
ollama list                     # doit contenir gemma3:4b
cp .env.example .env            # OLLAMA_ENABLED=true, OLLAMA_AUTO_APPLY=false
```

Gemma ne fait qu'analyser : ses recommandations, validées par une liste blanche, ne sont appliquées que si une mesure les confirme, et jamais sans aperçu en mode par défaut. Sur ce poste (CPU), une analyse prend environ 2 minutes par page. Détails et mesures : [docs/gemma3-scanning.md](docs/gemma3-scanning.md).

## Tests

```bash
.venv/bin/python -m pytest -q
# intégration avec le vrai modèle local (lent, facultatif)
BOOKOCR_TEST_OLLAMA=1 .venv/bin/python -m pytest -q -m ollama tests/test_ollama_integration.py
```

## Documentation

| Fichier | Contenu |
|---|---|
| [docs/audit.md](docs/audit.md) | Audit de l’existant et plan de migration |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Contrats des services et modèles |
| [docs/TECHNICAL_DECISIONS.md](docs/TECHNICAL_DECISIONS.md) | Décisions (OCR, persistance, modes DOCX) |
| [docs/pipeline.md](docs/pipeline.md) | Enchaînement des étapes |
| [docs/gemma3-scanning.md](docs/gemma3-scanning.md) | Numérisation intelligente, Ollama et Gemma 3, mesures réelles |
| [docs/quality-control.md](docs/quality-control.md) | Contrôles de qualité (reconstruction et numérisation) |
| [AGENTS.md](AGENTS.md) | Règles de développement |
| [planning.md](planning.md) | Tâches et états |
| [roadmap.md](roadmap.md) | Phases |
