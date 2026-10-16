# Stratégie de tests

Propriétaire : `qa-test-engineer`. Commande : `.venv/bin/python -m pytest -q -rs`.

## 1. Principes

- **Vérité terrain synthétique** : `tests/fixtures/synthetic.py` génère des pages dont le texte, les boîtes, les images, la rotation et l'inclinaison sont connus. Elles sont rendues avec une vraie police TTF (Liberation ou DejaVu), à 300 dpi.
- **Photos réelles** (marqueur `real_images`) : les 11 photos de `/home/garrix/Dev/LivreOCR_Web/media/books/2026/10/pages/` (5568 × 3712 px) sont **copiées** dans `tmp_path`. Le chemin est configurable par `BOOKOCR_REAL_PAGES`. Les tests sont ignorés si le dossier est absent. L'empreinte SHA-256 des originaux est vérifiée avant et après.
- **Ce que montre le corpus réel** (sondé le 16/10/2026 avec `ocr_probe.py` sur des copies de `00006.jpg` et `00021.jpg`) : banc de numérisation ; feuille occupant environ 40 % du cadre sur fond noir ; texte tourné de 90° ; reflets de 3 LED et page suivante visible en bas ; **texte allemand** ; pages des cas B (fond vert uni, grand titre) et C (photo et deux colonnes). Sur l'image brute, l'OSD échoue (« Too few characters ») et l'OCR donne une confiance de 31 avec un texte illisible. L'isolement de la feuille et l'orientation sont donc des préalables indispensables, et ces photos sont les tests de référence de T-20.
- **Les exports sont relus** : python-docx et PyMuPDF (`inspect_docx`, `validate_pdf`, scripts des skills). La seule existence d'un fichier ne valide rien.
- **Dépendances externes** : marqueurs `tesseract` (avec la langue requise) et `libreoffice`. Si l'outil manque, le test est `skip` avec une raison explicite.
- **Isolation** : `tmp_path` et `BOOKOCR_DATA_DIR` pointé sur `tmp_path` (fixture `data_dir`). Aucune écriture dans `data/` ni dans le dépôt.

## 2. Pages de référence (les trois cas obligatoires et leurs variantes)

| Fixture | Contenu | Vérité terrain |
|---|---|---|
| `text_page` | Cas A : titre, sous-titre, 4 paragraphes avec accents (é è ç œ « »), une liste, en-tête, numéro de page | texte par bloc, types, ordre |
| `two_column_page` | Titre pleine largeur et 2 colonnes | ordre de lecture, `columns == 2` |
| `graphic_background_page` | Cas B : dégradé et texture couvrant 70 % de la page, texte clair posé dessus | catégorie `graphic_background`, blocs `on_image`, régions de fond |
| `photo_text_page` | Cas C : photographie (motif naturel bruité) en haut, légende, 3 paragraphes dessous | boîte de l'image ±2 %, légende liée, paragraphes |
| `rotated_*` | `text_page` tournée de 90°, 180° et 270° | rotation détectée |
| `skewed_page` | inclinaison de 3° | angle à ±0,5° |
| `perspective_page` | feuille sur fond sombre et homographie | 4 coins à ±2 % |
| `clean_photo_page` | page déjà droite et nette | sortie quasi identique (écart moyen < 2 niveaux), pas de rognage |

## 3. Matrice exigences → tests

| # | Exigence (cahier des charges) | Test (fichier::fonction prévue) | Marqueurs |
|---|---|---|---|
| 1 | Correction des rotations 90/180/270 et aucune rotation d'une page droite | `test_orientation.py::test_quarter_turns_are_detected`, `::test_upright_page_is_not_rotated` | tesseract |
| 2 | Inclinaison, perspective, page nette conservée | `test_orientation.py::test_skew`, `::test_perspective_quad`, `::test_clean_page_untouched` | |
| 3 | Conservation des originaux | `test_project_service.py::test_originals_unchanged_after_full_pipeline` (SHA-256) | |
| 4 | Extraction du texte (accents, ponctuation) | `test_ocr_service.py::test_text_page_similarity` (≥ 0,9) | tesseract |
| 5 | Zones de faible confiance signalées | `test_ocr_service.py::test_low_confidence_words_flagged` | tesseract |
| 6 | Ordre de lecture et colonnes | `test_layout_service.py::test_reading_order_two_columns` | |
| 7 | Pas de bloc unique pour toute la page | `test_layout_service.py::test_paragraphs_are_separate` | |
| 8 | Trois catégories de pages | `test_layout_service.py::test_page_category[text|graphic|photo]` | |
| 9 | Extraction des images sans perte | `test_layout_service.py::test_illustration_extracted_lossless` | |
| 10 | Sauvegarde et rechargement d'un projet | `test_project_service.py::test_roundtrip_preserves_everything` (nouveau processus) | |
| 11 | Conservation des paramètres à la réouverture | `test_project_service.py::test_settings_survive_reopen` | |
| 12 | Historique et restauration d'une page | `test_project_service.py::test_restore_previous_version` | |
| 13 | Modification globale des polices, styles individuels préservés | `test_typography.py::test_global_font_change`, `::test_individual_override_preserved` | |
| 14 | DOCX ouvrable | `test_document_service.py::test_docx_opens[mode]` | |
| 15 | Texte réellement modifiable dans le DOCX | `test_document_service.py::test_text_is_real_paragraphs` | |
| 16 | Images extraites présentes dans le DOCX | `test_document_service.py::test_images_present` | |
| 17 | Mode modifiable sans image pleine page | `test_document_service.py::test_editable_has_no_full_page_image` | |
| 18 | Nombre de pages et sauts de page | `test_document_service.py::test_one_section_per_page`, `test_pdf_service.py::test_page_count` | |
| 19 | Conversion PDF (direct) | `test_pdf_service.py::test_direct_pdf_text_and_images` | |
| 20 | Conversion DOCX → PDF | `test_pdf_service.py::test_libreoffice_conversion_page_count` | libreoffice |
| 21 | Détection des débordements | `test_pdf_service.py::test_overflow_detected` | |
| 22 | Pas de page blanche involontaire | `test_pdf_service.py::test_no_blank_pages` | |
| 23 | Cache OCR (pas de recalcul si rien n'a changé) | `test_processing_service.py::test_ocr_cache_hit` | tesseract |
| 24 | Reprise après erreur | `test_processing_service.py::test_batch_continues_after_page_error` | |
| 25 | Import : formats, tailles, PDF multi-pages, TIFF multi-pages | `test_project_service.py::test_import_formats`, `::test_rejects_oversize` | |
| 26 | Interface : écrans, interactions principales, persistance | `test_ui.py` (AppTest) | ui |
| 27 | Bout en bout sur les trois cas | `test_integration.py::test_end_to_end[case]` : import → DOCX/PDF → relecture | slow |
| 28 | Photos réelles : orientation et OCR plausibles | `test_integration.py::test_real_pages` | real_images, slow |

## 4. Classement des échecs

Pour chaque échec, une catégorie : **défaut du code** (corrigé par le propriétaire), **test erroné** (corrigé avec justification), **limite de bibliothèque** (`xfail(reason=…)` et entrée dans KNOWN_LIMITATIONS), **environnement** (outil ou langue absents : `skip`).

## 5. Rapport

`docs/TEST_REPORT.md` est mis à jour à chaque fin de phase. Il contient la date, les commandes exactes, le nombre de tests passés, échoués et ignorés (avec les raisons), les mesures (temps par page, confiance moyenne sur les photos réelles) et les problèmes ouverts.
