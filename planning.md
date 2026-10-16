# Planification

Tenue par l'agent principal. Une tâche passe à `DONE` seulement quand ses critères sont vérifiés (test ou commande cités). Priorités : P0 bloquant, P1 nécessaire, P2 souhaitable.

| ID | Tâche | Phase | Prio | Dépend de | Propriétaire | Critères d'acceptation | État |
|---|---|---|---|---|---|---|---|
| T-01 | Audit du dépôt, de l'environnement et de l'ancien code ; configuration Claude Code (agents, skills, commandes, règles) ; ARCHITECTURE, TECHNICAL_DECISIONS, TEST_STRATEGY, KNOWN_LIMITATIONS | P0 | P0 | — | principal + software-architect | Agents et skills chargés par Claude Code ; scripts `ocr_probe`, `check_docx`, `check_pdf` exécutés sur de vrais fichiers | DONE (16/10 : `claude plugin validate` réussi pour les agents et les skills ; sonde OCR lancée sur 2 photos réelles copiées, SHA-256 inchangé ; `check_docx` et `check_pdf` testés sur un cas conforme, une attente fausse (code 1) et un fichier corrompu (code 2)) |
| T-02 | Dépendances : désinstaller `docx` 0.2.4, réinstaller `python-docx`, installer `pymupdf`, retirer `reportlab` et `pypdfium2` s'ils ne servent plus ; écrire `requirements.txt` | P1 | P0 | T-01 | integration-documentation | `pip check` sans erreur ; `import docx, pymupdf, cv2, streamlit` OK ; versions notées | DONE (16/10 : docx 0.2.4, reportlab, pypdfium2 retirés ; python-docx 1.2.0 et PyMuPDF 1.28.2 ; `pip check` OK ; requirements.txt écrit) |
| T-03 | `pyproject.toml` (configuration pytest, marqueurs), `config.py` (variables `BOOKOCR_*`), `.gitignore` (`data/`) | P1 | P0 | T-02 | integration-documentation | `pytest --markers` liste les marqueurs ; `config` importable sans effet de bord | DONE (16/10 : pyproject.toml avec 5 marqueurs, config.py, `data/` ignoré) |
| T-04 | `models/` selon ARCHITECTURE §6 et `tests/test_models.py` | P1 | P0 | T-01 | software-architect | Aller-retour JSON sans perte ; `schema_version` ; validation des Box (0..1) | DONE (16/10 : models/ v1, `tests/test_models.py` 11/11) |
| T-05 | `tests/fixtures/synthetic.py` : les pages du §2 de TEST_STRATEGY avec vérité terrain | P1 | P0 | T-04 | qa-test-engineer | Chaque fixture rend une image et une vérité terrain cohérentes (test de contrôle) | DONE (16/10 : `tests/fixtures/synthetic.py`, `tests/test_synthetic.py` 7/7, rendus vérifiés visuellement) |
| T-10 | `image_service` : lecture (EXIF, TIFF multi-pages, PDF via PyMuPDF), inspection d'import, vignettes, PNG protégé | P2 | P0 | T-04 | ocr-image-specialist | Tests formats ; refus d'écriture sous `originals/` | IN_PROGRESS (section 1 E/S faite par le principal, `tests/test_image_io.py` 9/9 ; section 2 confiée à ocr-image-specialist) |
| T-11 | `project_service` : créer, ouvrir, enregistrer (atomique et `.bak`), importer, réordonner, remplacer, supprimer (corbeille), rotation manuelle, instantanés et restauration | P2 | P0 | T-04, T-10 | integration-documentation | Exigences 3, 10, 11, 12 et 25 de TEST_STRATEGY | IN_PROGRESS |
| T-12 | Interface : navigation, tableau de bord, écran Pages (import, vignettes, ordre, rotation, remplacement, suppression), sauvegarde explicite et auto | P2 | P1 | T-11 | streamlit-ux-specialist | AppTest : écrans sans exception ; effets relus sur disque | TODO |
| T-20 | `orientation_service` : EXIF, OSD avec contrôle par lecture, feuille et perspective, inclinaison, incertitude signalée | P3 | P0 | T-05, T-10 | ocr-image-specialist | Exigences 1 et 2 | IN_PROGRESS |
| T-21 | `image_service` : amélioration (balance des blancs, luminosité, contraste, saturation, netteté, débruitage, ombres, lisibilité), image OCR | P3 | P1 | T-10 | ocr-image-specialist | Neutre = identité ; chaque réglage a un effet mesurable ; original intact | IN_PROGRESS |
| T-22 | Courbure (D-11), activable par page | P3 | P2 | T-20 | ocr-image-specialist | Page courbée synthétique améliorée ; page plate inchangée | IN_PROGRESS |
| T-23 | Interface : écran Correction (avant/après, rotation manuelle, réglages, avertissements) | P3 | P1 | T-12, T-20, T-21 | streamlit-ux-specialist | AppTest ; réglage enregistré et réappliqué | TODO |
| T-30 | Spike DOCX : cadres `framePr` ou zones de texte DrawingML, ancres d'image, colonnes de section ; conversion LibreOffice | P7 | P0 | T-02, T-40 | docx-pdf-specialist | Mesures de positions et de pages dans les deux variantes ; décision D-07 tranchée | BLOCKED (LibreOffice absent : la comparaison de rendu exige `soffice`) |
| T-31 | `ocr_service` : Tesseract TSV, langues, cache, seconde lecture, `ocr_region` | P4 | P0 | T-05 | ocr-image-specialist | Exigences 4, 5 et 23 | IN_PROGRESS |
| T-32 | `layout_service` : lignes, paragraphes, types, colonnes, ordre, catégories, illustrations, fonds, format détecté, fusion avec les corrections | P4 | P0 | T-31 | document-layout-specialist | Exigences 6, 7, 8 et 9 | IN_PROGRESS |
| T-33 | `processing_service` : pipeline par page, thread d'arrière-plan, `job.json`, reprise | P4 | P0 | T-20, T-31, T-32, T-11 | integration-documentation | Exigences 23 et 24 | TODO |
| T-34 | Interface : écran Traitement (langues, progression, erreurs) | P4 | P1 | T-33 | streamlit-ux-specialist | AppTest ; traitement non bloquant | TODO |
| T-35 | OCR allemand (le corpus réel est en allemand) | P4 | P0 | T-31 | utilisateur | `tesseract --list-langs` contient `deu` | BLOCKED (`sudo pacman -S tesseract-data-deu`, mot de passe requis) |
| T-40 | `composition_service` (trois modes) et `fonts` | P6 | P0 | T-32 | document-layout-specialist + docx-pdf-specialist (fonts) | Composition déterministe ; conversions d'échelle testées | IN_PROGRESS |
| T-41 | `typography_service` | P5 | P0 | T-40 | document-layout-specialist | Exigence 13 | IN_PROGRESS |
| T-42 | `pdf_service.render_preview` et `render_pdf` (direct) | P8 | P0 | T-40 | docx-pdf-specialist | Exigences 19, 21 et 22 | IN_PROGRESS |
| T-43 | Interface : éditeur de page (composant SVG de boîtes, styles par bloc, légendes, images), panneau typographique, écran Contrôle | P5 | P0 | T-41, T-42 | streamlit-ux-specialist | Chaque contrôle a un effet vérifié ; aperçu = rendu PDF | TODO |
| T-50 | `document_service.build_docx` (trois modes) et `inspect_docx` | P7 | P0 | T-40, T-30 | docx-pdf-specialist | Exigences 14 à 18 | IN_PROGRESS |
| T-51 | `pdf_service` : fac-similé recherchable, `convert_docx_to_pdf`, `validate_pdf` | P8 | P0 | T-42, T-50 | docx-pdf-specialist | Exigences 18 à 22 (20 : `skip` si LibreOffice est absent) | IN_PROGRESS |
| T-52 | Interface : écran Export (modes, format, aperçu PDF, rapport de validation, téléchargement) | P8 | P0 | T-50, T-51 | streamlit-ux-specialist | AppTest ; fichiers téléchargés relus | TODO |
| T-53 | Conversion DOCX → PDF réelle | P8 | P1 | T-51 | utilisateur | `soffice --version` répond | BLOCKED (`sudo pacman -S libreoffice-fresh`, mot de passe requis) |
| T-60 | Tests d'intégration des trois cas et des photos réelles ; TEST_REPORT | P9 | P0 | T-52 | qa-test-engineer | Exigences 27 et 28 | TODO |
| T-61 | README, USER_GUIDE, dépannage, installation dans un venv neuf, CHANGELOG | P10 | P0 | T-60 | integration-documentation | Commandes du README exécutées | TODO |

## Prochain lot

1. **T-02 puis T-03** (séquentiels : même propriétaire, `requirements.txt` et la configuration).
2. En parallèle de T-02 et T-03 : **T-04** (`models/`, software-architect). Les fichiers sont disjoints.
3. Puis **T-05** (fixtures) et **T-10** (image_service) en parallèle : fichiers disjoints, les deux dépendent seulement de T-04.

## Actions attendues de l'utilisateur

- `sudo pacman -S tesseract-data-deu` (OCR allemand, T-35). Autres langues au besoin : `tesseract-data-spa`, `tesseract-data-ita`…
- `sudo pacman -S libreoffice-fresh` (conversion et contrôle du rendu DOCX, T-30 et T-53).
