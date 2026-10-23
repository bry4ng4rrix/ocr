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

Écran **Traitement › Numérisation intelligente** : un PDF par livre. Chaque page montre la photographie redressée, recadrée et corrigée avec prudence, en couleurs, avec une couche OCR **invisible** et recherchable alignée mot par mot. Les boutons d'un même objectif sont regroupés à chaque étape.

1. **Livre et photos** : choisir un livre ou en créer un (langues OCR, par exemple `fra` et `deu`), ajouter toutes ses photos (téléversement ou dossier local), régler les options de traitement (OCR, suppression du fond, Gemma), puis « Importer et traiter le livre ».
2. **Traitement** : avancement en arrière-plan, une page à la fois, avec reprise après une interruption ; « Relancer les pages en erreur » et « Arrêter le traitement » côte à côte.
3. **Résultats** : galerie avant/après ou tableau, filtre par état, « Valider toutes les pages ».
4. **Retouche** : « Retoucher cette page » (fenêtre à outils : Aperçu, Recadrage, Réglages, Orientation et type, Analyse Gemma ; chaque outil porte ses propres boutons) ou « Modifier toutes les pages… » (recadrage commun, rognage, rotation).
5. **PDF du livre** : mise en page (format, orientation, marge, centrage) et options d'export, puis « Créer le PDF » ; le téléchargement s'affiche dès qu'il est prêt. Le PDF et son rapport (`.validation.json`) sont écrits dans `data/projects/<id>/exports/`.
6. **Statistiques et prix** des pages terminées.

**Sans IA**, c'est le fonctionnement par défaut : OpenCV et Tesseract font tout le travail.

**Avec Gemma 3 (Ollama local)** : le serveur et le modèle doivent déjà être installés, l'application ne télécharge rien.

```bash
ollama list                     # doit contenir gemma3:4b
cp .env.example .env            # OLLAMA_ENABLED=true, OLLAMA_AUTO_APPLY=false
```

Gemma ne fait qu'analyser : ses recommandations, validées par une liste blanche, ne sont appliquées que si une mesure les confirme, et jamais sans aperçu en mode par défaut. Sur ce poste (CPU), une analyse prend environ 2 minutes par page. Détails et mesures : [docs/gemma3-scanning.md](docs/gemma3-scanning.md).

## Docker et déploiement

```bash
docker compose up -d --build        # http://localhost:8501, données dans le volume Docker ocr-data
docker build --target test -t ocr-numerisation:test . && docker run --rm ocr-numerisation:test   # tests
```

CI/CD GitHub Actions (`.github/workflows/ci-cd.yml`) :
1. tests dans l'image Docker ;
2. image publiée sur `ghcr.io/<compte>/ocr` ;
3. déploiement SSH sur le VPS si les secrets `VPS_*` sont configurés.

Détails : [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

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
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | Docker, CI/CD, VPS |
| [docs/gemma3-scanning.md](docs/gemma3-scanning.md) | Numérisation intelligente, Ollama et Gemma 3, mesures réelles |
| [docs/quality-control.md](docs/quality-control.md) | Contrôles de qualité (reconstruction et numérisation) |
| [AGENTS.md](AGENTS.md) | Règles de développement |
| [planning.md](planning.md) | Tâches et états |
| [roadmap.md](roadmap.md) | Phases |
