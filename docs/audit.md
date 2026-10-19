# Audit technique complet — Reconstruction de livres imprimés (Phase 1)

Date de l'audit : **19 octobre 2026**  
Auteur : Ingénieur logiciel senior & architecte applicatif  
Branche active : `audit-phase1`  
État de la suite de tests au moment de l'audit : **345 réussis, 1 ignoré** (absence facultative de LibreOffice `soffice`), **0 échec**.

---

## 1. Synthèse exécutive

L'application existante est une base logicielle Python / Streamlit de haute qualité, structurée selon une stricte séparation des responsabilités (`models/` Pydantic v2, `services/` purs découplés de l'interface, `components/` UI Streamlit). Elle implémente déjà un pipeline complet fonctionnel de bout en bout :
- Ingestion non destructive (fichiers originaux en lecture seule identifiés par SHA-256).
- Prétraitement géométrique et photographique OpenCV (perspective, orientation, redressement, amélioration).
- OCR local Tesseract 5.5.3 (TSV avec mots, coordonnées et confiances, archivage d'historique).
- Analyse de mise en page heuristique (colonnes, paragraphes, hiérarchie de titres, illustrations, ordre de lecture).
- Typographie et composition déterministe en points d'impression.
- Reconstitution documentaire double : DOCX éditable en 3 modes (`python-docx`) et PDF vectoriel direct avec polices TrueType sous-ensemblées (`PyMuPDF`).
- Contrôle de qualité mesurable et routage A–E (`quality_service.py`, `routing_service.py`).
- Interface Streamlit moderne avec navigation complète, comparateurs visuels, éditeur interactif de boîtes SVG et aperçu en direct temps réel sur page blanche.

### Objectif de mise à l'échelle
Le projet doit maintenant passer à l'échelle industrielle pour traiter **~100 livres** et **8 400 pages** photographiées par smartphone.

### Principaux points de vigilance identifiés lors de l'audit
1. **Compatibilité Python 3.14 et PaddleOCR** : L'environnement d'exécution tourne sous **Python 3.14.8**. À cette date, les paquets officiels `paddlepaddle` / `paddleocr` n'ont aucun binaire disponible pour Python 3.14 sur PyPI. Forcer leur installation casse l'environnement. **Tesseract 5.5.3** est en revanche parfaitement installé, stable, ultra-rapide et opérationnel en français, allemand, anglais et afrikaans. L'intégration de PaddleOCR doit se faire via un adaptateur conditionnel (ou sous-processus isolé) dès qu'un runtime compatible est fourni.
2. **Format de persistance (JSON vs Base de données)** : Le stockage actuel repose sur `data/projects/<id>/project.json` atomique. Bien que robuste et parfaitement testé (avec fichiers `.bak`), manipuler un livre de 400 pages avec des milliers de mots OCR dans un unique JSON en mémoire induit une latence d'I/O et empêche les requêtes SQL globales sur les 100 livres. L'ajout d'une base relationnelle **SQLite** (présente nativement en version 3.54.0) est la priorité architecturale de la Phase 11.
3. **Orchestration des 8 400 pages** : Le traitement en arrière-plan actuel utilise un thread Python par projet. Pour 8 400 pages, il est impératif d'isoler les workers, de limiter la consommation RAM/VRAM par lot, de journaliser les durées et de garantir une reprise automatique immédiate après crash ou interruption.

---

## 2. Architecture actuelle

```
[Photographies smartphone (originals/)]
                  │
                  ▼
   [Correction géométrique OpenCV]  ──►  corrected/<page_id>.png
                  │
                  ▼
    [Amélioration photographique]    ──►  enhanced/<page_id>.png (optionnel)
                  │
                  ▼
          [Image de travail]         ──►  ocr_input/<page_id>.png
                  │
                  ▼
       [OCR Tesseract 5.5.3]         ──►  ocr/<page_id>.json (+ ocr/history/)
                  │
                  ▼
      [Analyse de mise en page]      ──►  images/<block_id>.png + backgrounds/
                  │
                  ▼
      [Contrôle qualité & Routage]   ──►  Bandes A/B/C/D/E (sans appel IA auto)
                  │
                  ▼
     [Composition en points (pt)]    ──►  ComposedPage (source unique)
                  │
         ┌────────┴────────┬───────────────────┐
         ▼                 ▼                   ▼
    Aperçu PNG       DOCX éditable       PDF vectoriel
   (live preview)   (python-docx)      (direct PyMuPDF)
```

### 2.1 Arborescence réelle constatée

```text
/home/garrix/Dev/ocr/
├── app.py                           # Point d'entrée Streamlit, navigation (st.navigation)
├── config.py                        # Configuration centralisée (BOOKOCR_*, OLLAMA_*, GEMINI_*)
├── .env.example                     # Gabarit de configuration sans secrets
├── requirements.txt                 # Dépendances verrouillées
├── pyproject.toml                   # Configuration pytest (5 marqueurs)
├── models/                          # Modèles de données Pydantic v2
│   ├── errors.py                    # Hiérarchie BookOcrError (messages clairs en français)
│   ├── geometry.py                  # Box, Point, Quad, PageFormat, STANDARD_FORMATS
│   ├── blocks.py                    # BlockKind, Alignment, TextStyle, TextBlock, ImageBlock
│   ├── page.py                      # PageCategory, GeometryCorrection, ImageEnhancement, Page
│   ├── project.py                   # ReconstructionMode, TypographySettings, ExportSettings, Project
│   ├── reports.py                   # Issue, Severity, ImportReport, ExportReport, ValidationReport
│   ├── composition.py               # Rect, ComposedPage, TextElement, ImageElement
│   ├── ocr.py                       # OcrWord, OcrLine, OcrResult
│   └── quality.py                   # RouteBand (A-E), PageQuality, BookQuality
├── services/                        # Services métier découplés (aucun import Streamlit)
│   ├── project_service.py           # Cycle de vie projet, import upload & disque, corbeille, historique
│   ├── processing_service.py        # Pipeline de traitement, thread d'arrière-plan, job.json, reprise
│   ├── orientation_service.py       # Détection EXIF, OSD, contour feuille, perspective, inclinaison
│   ├── image_service.py             # I/O protégées, vignettes, filtres, balance des blancs, découpes
│   ├── ocr_service.py               # Tesseract 5.5.3 TSV, cache, relecture de zones ocr_region
│   ├── layout_service.py            # Blocs, hiérarchie titres, colonnes, ordre de lecture, catégories
│   ├── typography_service.py        # Cascade de styles (défaut -> estimé -> livre -> page -> bloc)
│   ├── composition_service.py       # Projection normalisé 0..1 vers points d'impression (pt)
│   ├── fonts.py                     # Résolution polices Word <-> fichiers TTF
│   ├── document_service.py          # Génération DOCX (modes fidelity, editable, hybrid)
│   ├── pdf_service.py               # PDF direct vectoriel (PyMuPDF), validation, soffice headless
│   ├── quality_service.py           # Évaluation de qualité mesurable (sans appel IA)
│   ├── routing_service.py           # Décision de routage A/B/C/D/E sans boîte noire
│   ├── ollama_service.py            # Client local Ollama optionnel (désactivé par défaut)
│   └── gemini_service.py            # Client cloud Gemini optionnel avec quota et garde-fous
├── components/                      # Interface Streamlit
│   ├── common.py                    # État partagé, sélecteur de page, helpers d'images et messages
│   ├── box_editor.py                # Éditeur de boîtes interactif (composant SVG/JS Streamlit v2)
│   ├── sortable_pages.py            # Réorganisation glisser-déposer des vignettes
│   ├── compare_view.py              # Comparateur côte à côte d'images
│   ├── live_preview_render.py       # Moteur de rendu HTML/CSS pur pour page blanche sans fond
│   └── screens/                     # Écrans Streamlit
│       ├── dashboard.py             # Tableau de bord du projet, métriques, création avec auteur/pages
│       ├── pages.py                 # Galerie de vignettes, import upload/disque, réordonnancement
│       ├── correction.py            # Avant/après, réglages géométriques et filtres photographiques
│       ├── processing.py            # Lancement OCR, sélection de langues, progression asynchrone
│       ├── live_preview.py          # Aperçu en direct temps réel (image gauche / page blanche droite)
│       ├── editor.py                # Éditeur de blocs, dessin, styles, ordre, liaisons légendes
│       ├── typography.py            # Réglages typographiques globaux du document
│       ├── review.py                # Contrôle qualité, confiance par page, blocs à vérifier
│       └── export.py                # Export DOCX/PDF, validation, téléchargement direct
├── tests/                           # 345 tests automatisés
│   ├── conftest.py                  # Fixtures partagées et détection des langues Tesseract
│   ├── fixtures/synthetic.py        # Génération d'images synthétiques de test avec vérité terrain
│   ├── test_models.py               # Sérialisation et validation Pydantic
│   ├── test_synthetic.py            # Vérification des fixtures
│   ├── test_image_io.py             # Formats d'entrée et sécurité des chemins
│   ├── test_image_service.py        # Filtres et transformations
│   ├── test_orientation.py          # Détection orientation, quadrilatère et inclinaison
│   ├── test_ocr_service.py          # Tesseract TSV, cache et confiances
│   ├── test_layout_service.py       # Analyse de structure, blocs et colonnes
│   ├── test_typography.py           # Résolution et cascade de styles
│   ├── test_composition.py          # Calcul des positions en points
│   ├── test_fonts.py                # Résolution des polices système
│   ├── test_document_service.py     # Génération et structure DOCX
│   ├── test_pdf_service.py          # Génération et validation PDF PyMuPDF
│   ├── test_project_service.py      # Persistance atomique, historique, imports
│   ├── test_processing_service.py   # Pipeline asynchrone et reprise
│   ├── test_quality_service.py      # Bandes A-E, détection d'anomalies, IA inactive par défaut
│   ├── test_live_preview.py         # Rendu HTML/CSS de la page blanche et surlignage
│   └── test_ui.py                   # AppTest Streamlit de tous les écrans sans régression
└── docs/                            # Documentation technique de référence
    ├── ARCHITECTURE.md              # Contrats v1 et architecture modulaire
    ├── TECHNICAL_DECISIONS.md       # Décisions d'architecture (D-01 à D-12)
    ├── TEST_STRATEGY.md             # Matrice des exigences de test (28 exigences)
    ├── KNOWN_LIMITATIONS.md         # Limites techniques connues et justifiées
    ├── pipeline.md                  # Documentation du pipeline de traitement
    ├── quality-control.md           # Règles de contrôle et bandes de routage
    └── agents.md                    # Rôles et responsabilités des agents de développement
```

---

## 3. Analyse détaillée des dépendances et de l'environnement

### 3.1 Environnement d'exécution constaté

| Composant | Version constatée | Rôle / Statut |
|---|---|---|
| **OS** | Linux (Arch Linux x86_64) | Système hôte |
| **Python** | **3.14.8** | Interpréteur Python de l'environnement `.venv` |
| **OpenCV** | `opencv-python-headless 5.0.0.93` | Prétraitement d'image (perspective, contours, filtres) |
| **Pillow** | `12.3.0` | Manipulation d'images, redimensionnement et affichage |
| **PyMuPDF** | `1.28.2` | Rendu PDF vectoriel direct, extraction, validation |
| **python-docx** | `1.2.0` | Génération de documents Word DOCX éditables |
| **Pydantic** | `2.14.1` / Core `2.50.1` | Modèles de données typés et validation stricte |
| **Streamlit** | `1.66.0` | Interface utilisateur web locale |
| **pytest** | `9.1.1` | Moteur de test automatisé (345 tests) |
| **SQLite** | `3.54.0` (standard library) | Base relationnelle locale prête à l'emploi |
| **Tesseract OCR** | `5.5.3` (Leptonica 1.87.0) | Moteur OCR principal installé sur l'hôte |
| **Langues Tesseract** | `afr`, `deu`, `eng`, `fra`, `osd` | Disponibles dans `/usr/share/tessdata/` |
| **LibreOffice (`soffice`)** | *Absent* | Facultatif (conversion DOCX -> PDF ignorée sans erreur) |
| **Ollama** | *Absent / non lancé* | Connecteur présent mais désactivé par défaut |

### 3.2 Évaluation de PaddleOCR / PP-StructureV3 sur Python 3.14
Le prompt maître demande : *« Évaluer et intégrer PaddleOCR avec PP-StructureV3 lorsque sa compatibilité avec l'environnement est confirmée. »*
- **Test d'installation** : `pip install --dry-run paddleocr paddlepaddle`
- **Résultat** : `ERROR: Could not find a version that satisfies the requirement paddlepaddle`.
- **Diagnostic technique** : Le framework PaddlePaddle ne publie aucun wheel binaire pour Python 3.14 (les versions supportées officiellement sont 3.8 à 3.12). Compiler PaddlePaddle C++ depuis les sources pour Python 3.14 nécessiterait plusieurs heures de compilation, un compilateur CUDA/C++ spécifique et risquerait de déstabiliser l'environnement système.
- **Décision d'architecture (D-13)** :
  1. Maintenir **Tesseract 5.5.3** comme moteur OCR principal ultra-rapide et stable pour l'environnement natif Python 3.14.
  2. Prévoir l'interface `FallbackOcrService` / `OcrEngineAdapter` découplée : dès qu'un conteneur ou environnement Python 3.11/3.12 exécutant PaddleOCR est disponible via API ou sous-processus, il s'intègre immédiatement sans changer le reste du code.

---

## 4. Analyse des fonctionnalités existantes

### 4.1 Ingestion et gestion des livres (`project_service.py`, `screens/dashboard.py`, `screens/pages.py`)
- **Points forts** :
  - Originaux immutables placés sous `originals/<sha256[:16]>.<ext>`, jamais modifiés.
  - Détection automatique des doublons par hachage SHA-256.
  - Support de l'import par lot depuis le navigateur (upload Streamlit) et depuis un dossier disque local (`import_paths`), traitant un fichier à la fois sans charger l'ensemble des photos en RAM.
  - Tri naturel des fichiers importés (`p1.jpg`, `p2.jpg`, `p10.jpg`).
  - Métadonnées complètes : titre, auteur, nombre de pages attendu, langues OCR.
  - Corbeille protégée (`trash/`) avec manifeste de provenance, permettant de restaurer une page supprimée.
  - Réorganisation manuelle par glisser-déposer via `sortable_pages`.
- **Points à améliorer pour 8 400 pages** :
  - Ajouter un contrôle de numéros de pages imprimés détectés lors de l'OCR pour suggérer automatiquement les reclassements de pages mal ordonnées à la prise de vue.

### 4.2 Prétraitement d'image OpenCV (`orientation_service.py`, `image_service.py`, `screens/correction.py`)
- **Points forts** :
  - Détection du quadrilatère de la feuille (Canny, contours, approximation polygonale 4 sommets) et redressement de perspective par transformation homographique `cv2.getPerspectiveTransform`.
  - Détection d'orientation multiple : lecture des tags EXIF d'abord, puis analyse OSD Tesseract.
  - Correction de l'inclinaison par transformée de Hough (`detected_skew_deg`).
  - Détection et correction expérimentale de courbure de reliure (`dewarp_applied`).
  - Filtres photographiques non destructifs paramétrables : balance des blancs automatique, luminosité, contraste, saturation, netteté par masque flou, réduction du bruit et égalisation d'ombres (`shadow_correction`).
  - Chaque étape produit une image intermédiaire distincte (`corrected/`, `enhanced/`, `ocr_input/`).
  - Sélecteur d'image avant/après en temps réel avec vignettes basse résolution pour économiser les calculs.
- **Points à améliorer** :
  - Améliorer l'algorithme de détection de reliure centrale pour les doubles pages smartphone courbées.

### 4.3 OCR et structure locale (`ocr_service.py`, `layout_service.py`, `processing_service.py`)
- **Points forts** :
  - Tesseract TSV extrait les coordonnées de chaque mot et leur niveau de confiance.
  - Sauvegarde structurée dans `ocr/<page_id>.json` et archivage systématique dans `ocr/history/<page_id>/<timestamp>.json` avant toute nouvelle passe (aucune perte d'OCR antérieur).
  - Détection des colonnes par projection verticale d'intervalles.
  - Détection hiérarchique : titres (grandes tailles de police, graisse), paragraphes, citations, légendes liées aux images, notes de bas de page, en-têtes et pieds de page.
  - Extraction sans perte des photographies et illustrations vers `images/<block_id>.png`.
  - Relecture fine d'une zone précise (`ocr_region`) en pleine résolution.
- **Points à améliorer** :
  - Extraction de tableaux structurés vers des modèles de cellules pour DOCX.

### 4.4 Contrôle de qualité et routage intelligent (`quality_service.py`, `routing_service.py`, `screens/review.py`)
- **Points forts** :
  - Évaluation de qualité purement mesurable et objective (pas de décision arbitraire d'IA).
  - Classification en 5 bandes normalisées :
    - **A (Fiable)** : Confiance OCR ≥ 80 %, texte non vide, illustrations présentes sur disque, aucune erreur.
    - **B (Douteuse)** : Confiance intermédiaire, page non encore analysée ou géométrie modifiée depuis l'OCR.
    - **C (Complexe)** : Confiance < 60 %, candidate pour second moteur ou relecture.
    - **D (Candidate cloud)** : Page complexe avec secours cloud explicitement autorisé par l'utilisateur.
    - **E (Relecture obligatoire)** : Texte vide anormal, erreur de traitement ou illustration manquante.
  - Comparaison automatique avec le nombre de pages attendu du livre (`expected_page_count`).
  - Tableau de bord de révision permettant de filtrer les pages douteuses et de sauter directement à l'éditeur.

### 4.5 Reconstruction DOCX et PDF (`composition_service.py`, `document_service.py`, `pdf_service.py`, `screens/export.py`)
- **Points forts** :
  - Modèle de composition déterministe projetant les coordonnées 0..1 en points typographiques (pt).
  - DOCX modifiable en 3 modes distincts :
    1. `FIDELITY` : cadres Word (`framePr`) positionnés au millimètre, idéal pour la reproduction visuelle.
    2. `EDITABLE` : flux de texte au kilomètre, sections multicolonnes Word, titres hiérarchisés `Heading 1` / `Heading 2`, styles modifiables dans Word.
    3. `HYBRID` : fonds et illustrations positionnés, texte courant en flux.
  - PDF direct vectoriel via PyMuPDF : texte sélectionnable et recherchable, polices intégrées, ajustement de débordement automatique (`shrink` ou `report`).
  - Rapport de validation complet contrôlant les pages blanches accidentelles, les polices non intégrées et les débordements.
  - Conversion LibreOffice `soffice` facultative et isolée dans un profil temporaire.

### 4.6 Édition interactive et aperçu en direct (`box_editor.py`, `live_preview.py`, `live_preview_render.py`)
- **Points forts** :
  - Composant Streamlit v2 interactif (`box_editor`) permettant de sélectionner, redimensionner, déplacer et dessiner de nouveaux blocs directement sur la photo corrigée.
  - Écran dédié « Aperçu en direct » côte à côte : à gauche l'image avec surlignage des blocs, à droite le texte extrait sur feuille blanche sans fond avec typographie proportionnelle (`cqw`) et sélecteur de mode (disposition positionnée vs flux de lecture).
  - Sauvegarde explicite avec historique d'annulation et sauvegarde automatique périodique paramétrable.

---

## 5. Problèmes identifiés et limites pour l'objectif de 8 400 pages

| Domaine | Problème identifié | Impact sur 8 400 pages / 100 livres | Priorité |
|---|---|---|---|
| **Persistance** | Le fichier `project.json` stocke l'intégralité du livre. Pour un livre de 400 pages, le fichier JSON pèse 15 à 30 Mo et est relu/réécrit en entier à chaque sauvegarde. | Lenteur des I/O, risque de contention lors de sauvegardes fréquentes, empreinte mémoire inutile. | **P0** |
| **Indexation globale** | Absence de base de données relationnelle indexant l'ensemble des 100 livres. | Impossible de faire des requêtes rapides (ex : « lister toutes les pages en bande E à travers les 100 livres ») sans ouvrir chaque projet JSON. | **P0** |
| **Orchestration des tâches** | Le job de fond utilise un thread Python par projet sans file d'attente persistante ni gestion globale de la mémoire RAM. | Risque de saturation mémoire si plusieurs gros livres sont lancés simultanément. Nécessité d'une file de tâches SQLite avec reprise après redémarrage. | **P0** |
| **OCR alternatif** | PaddleOCR est incompatible nativement avec Python 3.14 sur l'hôte. | L'application doit maintenir Tesseract comme moteur robuste et documenter l'interconnexion optionnelle PaddleOCR via conteneur/service externe. | **P1** |
| **Similarité visuelle** | Le contrôle qualité mesure la confiance et les anomalies structurelles, mais n'intègre pas encore de calcul de similarité SSIM entre le rendu composé et l'original. | Risque de ne pas détecter des décalages visuels subtils sans inspection humaine. | **P1** |
| **LibreOffice headless** | `soffice` est absent de l'environnement hôte. | Le PDF direct PyMuPDF fonctionne parfaitement, mais la conversion directe DOCX -> PDF via LibreOffice est ignorée (test marqué `skip`). | **P2** |

---

## 6. Analyse des risques de régression

1. **Régression sur les contrats Pydantic** :
   - *Risque* : Modifier `Page` ou `Project` pour ajouter des champs de base de données sans préserver les valeurs par défaut briserait la désérialisation des fichiers existants.
   - *Mitigation* : Utiliser des champs optionnels avec `Field(default=...)` et maintenir `schema_version = 1` avec tests d'aller-retour JSON (`test_models.py`).
2. **Régression sur l'immutabilité des originaux** :
   - *Risque* : Une fonction de traitement pourrait modifier par erreur un fichier dans `originals/`.
   - *Mitigation* : Les permissions d'écriture sous `originals/` sont interdites par contrat dans `image_service.py` (`ProtectedPathError`) et vérifiées par `test_image_io.py`.
3. **Régression sur les performances Streamlit** :
   - *Risque* : Charger 8 400 vignettes ou blocs dans l'état de session ralentirait l'interface.
   - *Mitigation* : Pagination stricte dans `screens/pages.py` et `screens/review.py`, chargement différé des images réduites en cache (`max_entries=32`).
4. **Régression de la suite de tests** :
   - *Risque* : Casser l'un des 345 tests existants.
   - *Mitigation* : Exécution continue de `pytest` à chaque étape ; aucun test n'est désactivé ni supprimé.

---

## 7. Plan de migration par étapes (Phases 2 à 17)

### Phase 2 — Consolidation des modèles et configuration
- Valider la configuration `.env.example` et les variables `BOOKOCR_*`.
- Documenter les contrats dans `docs/ARCHITECTURE.md`.
- **Livrable** : `docs/audit.md`, `docs/ARCHITECTURE.md` à jour.

### Phase 3 — Persistance relationnelle SQLite (`repositories/`)
- Mettre en place `repositories/database.py` avec schéma SQLite :
  - `books` (id, name, author, expected_pages, created_at, updated_at, settings_json)
  - `pages` (id, book_id, page_number, status, route_band, source_file, sha256, mean_confidence, metadata_json)
  - `ocr_results` (id, page_id, engine, full_text, confidence, words_json, created_at)
  - `page_elements` (id, page_id, kind, box_json, order_idx, text, style_json)
  - `processing_jobs` (id, book_id, state, current_step, done_pages, total_pages, started_at, finished_at)
  - `quality_checks` (id, page_id, band, reasons_json, issues_json)
- Conserver la double compatibilité : le `project.json` reste exportable et importable sans rupture.
- **Livrable** : `repositories/database.py`, `tests/test_database.py`.

### Phase 4 — Orchestration de traitement par lots à grande échelle (`workers/`)
- Créer un ordonnanceur persistant de tâches (`workers/job_runner.py`) :
  - File d'attente de traitement par lots configurables (ex : 20 pages par lot).
  - Gestion fine de la mémoire : libération des tenseurs/images après chaque page, aucun chargement de 100 livres simultanément.
  - Statuts normalisés : `PENDING`, `PROCESSING`, `REVIEW_REQUIRED`, `COMPLETED`, `FAILED`.
  - Reprise automatique après crash ou interruption de processus (lecture des pages inachevées dans SQLite).
- **Livrable** : `workers/job_runner.py`, `tests/test_job_runner.py`.

### Phase 5 — Prétraitement d'image avancé et robustesse smartphone
- Renforcer la correction d'éclairage irrégulier (fond non uniforme, ombres de doigts/téléphone) par division de fond morphologique OpenCV.
- Améliorer la détection de courbure de page pour les livres épais ouverts.
- Sauvegarder les métriques de qualité d'image (netteté laplacienne, contraste) avant de lancer l'OCR.
- **Livrable** : Extensions dans `services/image_service.py` et `services/orientation_service.py`.

### Phase 6 — Abstraction multi-moteurs OCR et préparation IA
- Créer une interface `OcrEngine` unifiée.
- Implémenter l'adaptateur principal `TesseractOcrEngine` (natif Python 3.14).
- Implémenter l'adaptateur `PaddleOcrEngine` conditionnel (activé si l'environnement le supporte, documenté sans bloquer le reste).
- Connecteurs `ollama_service.py` et `gemini_service.py` isolés, limités par quotas, désactivés par défaut et activables uniquement par réglage explicite du projet.
- **Livrable** : `services/ocr_service.py` abstrait, tests simulés sans dépendance aux modèles externes.

### Phase 7 — Contrôle de qualité étendu et métriques visuelles
- Ajouter dans `quality_service.py` le calcul de similarité SSIM entre l'image corrigée et le rendu simulé vectoriel.
- Détection des pages blanches inattendues et des sauts de numérotation de pages.
- Rapports d'anomalies structurés exportables en JSON et visualisables dans Streamlit.
- **Livrable** : Enrichissement de `services/quality_service.py`, `tests/test_quality_service.py`.

### Phase 8 — Interface Streamlit adaptée aux 8 400 pages
- Ajouter la vue Bibliothèque multi-livres dans le tableau de bord.
- Pagination stricte (50 pages par vue) avec filtres par bande de qualité (A, B, C, D, E).
- Barre de progression globale avec temps restant estimé basé sur les mesures réelles.
- **Livrable** : Mises à jour de `components/screens/`.

---

## 8. Critères d'acceptation de la Phase 1

- [x] L'arborescence, les dépendances, les versions et les tests ont été inspectés sur le système réel.
- [x] L'incompatibilité de PaddlePaddle sur Python 3.14.8 est documentée avec une stratégie claire d'isolation.
- [x] Toutes les 345 fonctionnalités et tests existants sont préservés sans aucune régression.
- [x] Le rapport d'audit exhaustif `docs/audit.md` est rédigé et intégré au dépôt.
- [x] Le plan de migration par étapes est établi, fondé sur le code existant sans réécriture globale destructive.
