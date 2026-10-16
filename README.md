# LivreOCR Streamlit

Application locale (Python + Streamlit) qui reconstruit un livre en PDF à partir de photos ou de scans de ses pages :

- prétraitement non destructif : feuille, perspective, orientation, inclinaison, marges, réglages photométriques globaux et locaux ;
- OCR Tesseract avec coordonnées, confiance et faible confiance signalée ;
- analyse de mise en page : titres, paragraphes, colonnes, ordre de lecture, légendes, en-têtes, numéros ;
- extraction des illustrations en PNG sans perte ;
- éditeur d'images et éditeur de blocs (souris et champs numériques), annuler et rétablir ;
- **PDF reconstruit** : vrai texte sélectionnable, polices intégrées, illustrations séparées et positionnées ;
- **PDF fac-similé** : images des pages, avec couche de texte invisible.

Elle succède à l'application Django `LivreOCR_Web` (voir [AUDIT.md](AUDIT.md)). Sans comptes ni authentification : tout reste sur la machine et aucun document n'est envoyé à un service externe.

## Installation (Linux)

### 1. Dépendances système : Tesseract et ses langues

Debian ou Ubuntu :

```bash
sudo apt update
sudo apt install -y tesseract-ocr tesseract-ocr-fra tesseract-ocr-eng tesseract-ocr-osd fonts-liberation fonts-dejavu-core
# autres langues : tesseract-ocr-deu, tesseract-ocr-spa, tesseract-ocr-ita…
```

Arch Linux :

```bash
sudo pacman -S tesseract tesseract-data-fra tesseract-data-eng tesseract-data-osd ttf-liberation ttf-dejavu
# autres langues : tesseract-data-deu…
```

Fedora : `sudo dnf install tesseract tesseract-langpack-fra tesseract-langpack-eng liberation-fonts`.

Vérification :

```bash
tesseract --version
tesseract --list-langs      # doit lister fra, eng et osd (osd sert à l'orientation automatique)
```

Les modèles sont dans `/usr/share/tessdata` (Arch) ou `/usr/share/tesseract-ocr/5/tessdata` (Debian). Si Tesseract ou une langue manque, l'application l'indique avec la commande d'installation ; l'import, l'éditeur d'images et le PDF restent utilisables.

Polices : le PDF intègre des polices TrueType libres (Liberation, DejaVu ou Noto) trouvées dans `assets/fonts/` puis dans `/usr/share/fonts`. Pour forcer une police, copiez ses fichiers `.ttf` dans `assets/fonts/`.

### 2. Environnement Python (3.12 ou plus récent ; testé avec 3.14)

```bash
cd /home/garrix/Dev/ocr
python -m venv .venv            # ou : uv venv .venv
source .venv/bin/activate
pip install -r requirements.txt # ou : uv pip install -r requirements.txt
```

### 3. Lancement

```bash
streamlit run app.py
```

Ouvrez ensuite l'adresse affichée (par défaut http://localhost:8501).

## Utilisation

1. **Projet** (barre latérale) : créez un livre ou rouvrez-en un.
2. **Pages** : importez plusieurs fichiers (JPG, PNG, TIFF multi-pages, PDF). Réordonnez, faites pivoter, supprimez ou ajoutez des pages. Les originaux ne sont jamais modifiés.
3. **Éditeur d'image** : géométrie (recadrage de la feuille, coins manuels, redressement, marges, échelle), mode (texte A, illustration B, mixte C), réglages **pour l'impression** et **pour l'OCR** (deux jeux distincts), corrections locales rectangulaires, réglages propres à chaque illustration (brouillon, puis « Valider pour le PDF »). Les aperçus avant et après sont recalculés depuis l'original.
4. **Traitement OCR** : choisissez les langues installées et lancez l'OCR. Les blocs que vous avez corrigés ou créés sont conservés lors d'une relance.
5. **Éditeur de blocs** : corrigez le texte (les mots de faible confiance sont signalés), les styles, la position et la taille (à la souris ou au clavier), l'ordre de lecture ; ajoutez ou supprimez des blocs. L'aperçu « reconstruit » est le PDF lui-même rasterisé.
6. **Export PDF** : mode B (reconstruction modifiable) ou mode A (fac-similé). Le rapport relit le PDF : texte extractible, images, tailles de page, pages non reconstruites.

### Sauvegarde et restauration

- Chaque modification est enregistrée dans `workspace/projects/<id>/project.json` (JSON versionné ; la version précédente est gardée dans `project.json.bak`). Un projet se rouvre sans relancer l'OCR.
- **Sauvegarde portable** : barre latérale, « Télécharger la sauvegarde (.zip) » : project.json, originaux, illustrations et exports.
- **Restauration** : barre latérale, « Restaurer une sauvegarde » : le ZIP est vérifié (chemins, taille, schéma) et ouvert comme nouveau projet.

## Organisation du code

```
app.py                  point d'entrée Streamlit (navigation)
config.py               limites et chemins (variables LIVREOCR_*)
core/                   calcul pur, sans Streamlit
  document_models.py    modèle typé (Pydantic) : Project > Page > TextBlock / ImageBlock
  coordinates.py        repère normalisé 0..1 du cadre ↔ pixels ↔ points PDF
  image_loader.py       lecture et validation des fichiers importés
  image_preprocessing.py géométrie, modes A/B/C, image OCR, image d'impression
  image_adjustments.py  réglages photométriques globaux et locaux
  ocr_engine.py         Tesseract (TSV), langues, orientation
  layout_analysis.py    blocs, types, colonnes, ordre de lecture, styles
  illustration_extraction.py  détection et découpe des illustrations
  fonts.py, pdf_generator.py, pdf_validator.py  PDF (ReportLab) et relecture (pypdfium2)
services/               orchestration : stockage, projet, traitement, export, historique
ui/                     écrans Streamlit et composant d'édition des blocs
tests/                  pytest (pages synthétiques avec vérité terrain)
workspace/              projets locaux (exclu de Git)
```

Le dossier `workspace/` regroupe, par projet, les originaux, les images de travail, les illustrations et les exports, au lieu des dossiers globaux `uploads/` et `extracted_images/` suggérés par le cahier des charges. Ainsi, un projet se sauvegarde, se restaure ou se supprime d'un bloc, et ses chemins relatifs restent valides.

## Tests

```bash
.venv/bin/python -m pytest -q
```

Les tests OCR exigent Tesseract avec `fra` et `eng` ; ils sont ignorés et signalés comme tels sinon. Le rapport des tests réellement exécutés est dans [TEST_REPORT.md](TEST_REPORT.md).

## Configuration

| Variable | Défaut | Rôle |
|---|---|---|
| `LIVREOCR_WORKSPACE` | `./workspace` | dossier des projets et des journaux |
| `LIVREOCR_MAX_FILE_MB` | 60 | taille maximale d'un fichier importé |
| `LIVREOCR_MAX_PAGES` | 800 | pages maximum par projet |
| `LIVREOCR_MAX_IMAGE_PIXELS` | 150 000 000 | protection contre les images démesurées |
| `LIVREOCR_PDF_IMPORT_DPI` | 300 | résolution de rasterisation des PDF importés |
| `LIVREOCR_TESSERACT` | `tesseract` | chemin du binaire |
| `LIVREOCR_OCR_TIMEOUT` | 300 | délai maximal d'OCR d'une page (s) |
| `LIVREOCR_FONT_DIRS` | `assets/fonts:/usr/share/fonts:…` | dossiers de polices |

Journaux : `workspace/logs/livreocr.log`.

## Limites connues

- La police, la taille, le gras et l'italique sont **estimés** : le PDF reconstruit reproduit les blocs détectés, pas la typographie exacte du livre. Tout reste modifiable avant l'export.
- Les tableaux sont rendus en texte (retours à la ligne conservés), sans les filets ni les cellules.
- La sélection des corrections locales est rectangulaire (pas de lasso).
- La qualité de l'OCR dépend des langues installées : un livre en allemand demande `deu`.
