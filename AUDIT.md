# Audit — de LivreOCR_Web (Django) vers LivreOCR Streamlit

Audit réalisé le 16/10/2026 sur `/home/garrix/Dev/LivreOCR_Web`, en **lecture seule**. Aucun fichier de l'ancien projet n'a été modifié. Les tests Django n'ont pas été lancés : ils écrivent dans la base et dans `media/`, ce qui aurait modifié l'ancien projet.

Légende : **[V]** fait vérifié dans le code ou sur le disque, **[H]** hypothèse ou appréciation non vérifiée par exécution.

## 1. Ancien projet : état constaté

| Élément | Constat |
|---|---|
| Dépôt | Git, dernier commit `72fe6b5 setting`. **Arbre de travail modifié et non commité** (README, models, ocr, image_processing, tests…) [V]. L'audit porte sur les fichiers tels qu'ils sont sur le disque. |
| Pile | Django 5.2, Pillow, OpenCV, NumPy, ReportLab, pypdfium2, pdfminer.six, pikepdf, OCRmyPDF, Tesseract en sous-processus [V] |
| Taille | ≈ 8 200 lignes (Python, JS, gabarits) ; une application `books/` [V] |
| Données | `db.sqlite3`, 11 photos de pages (5568 × 3712 px, JPEG) et 2 PDF dans `media/` [V] |
| Documentation | `docs/audit-technique.md` est **périmé** : il affirme que « le texte OCR n'est stocké nulle part » et que l'OCR est synchrone, alors que le code contient désormais `PageElement`, `SourceImage`, `ProcessingJob` et un worker [V] |

### Architecture [V]

- `models.py` : `Book` (≈ 60 réglages), `PageImage`, `PageElement` (zone typée, boîte en fractions 0..1 du « cadre de page », `ocr_text` et `corrected_text` séparés, `estimated`, `style`), `SourceImage` et `ImageOccurrence` (illustration et placement), `PdfExport`, `ProcessingJob`.
- `image_processing.py` : pipeline non destructif à partir de l'original : EXIF, détection de la feuille (`detect_page_quad`, Otsu et `minAreaRect`), redressement de perspective, rotation 0/90/180/270, rognage des bords, redressement fin par profil de projection (±5°), puis réglages tonals et placement A4.
- `orientation.py` : OSD Tesseract, contrôlé par une lecture réelle (`--psm 6`) quand l'OSD est peu sûr.
- `color.py` : masque « image » en Lab (grandes zones à forte variance de tons ou saturées) et profils de restauration des couleurs.
- `layout.py` : zones de la page. Images (`picture_mask` et dessins au trait), tableaux (réglures horizontales et verticales), paragraphes Tesseract (TSV), classification par règles (titre, légende, liste, en-tête, pied, numéro), alignement, gras (épaisseur du trait), couleur de l'encre, ordre de lecture par bandes et colonnes.
- `rebuild.py` : mode B avec ReportLab. Pages blanches, texte réel en police TTF intégrée, réduction automatique de la taille (jusqu'à 60 %), extension du bloc vers le bas, sinon découpe signalée. Images placées, chevauchements comptés.
- `ocr.py` : mode A. Images de page, plus une couche de texte invisible produite par OCRmyPDF ou tirée du texte corrigé.
- `validate.py` : relit le PDF reconstruit (pdfminer) et compare le texte et la position de chaque bloc.
- `toc.py` : table des matières cliquable (pikepdf).
- Interface : gabarits Django et JS vanilla (`boxes.js` : déplacement et redimensionnement des zones à la souris en SVG ; `crop.js` : 4 coins).
- Asynchrone : table `ProcessingJob` et commande `run_worker`.

### Pipeline de bout en bout [V]

1. **Import** (`views.upload_pages`, `services.add_page`) : extensions autorisées, taille maximale (30 Mo par image, 200 Mo par PDF), format réel vérifié par Pillow, limite en pixels, doublons détectés par SHA-256. Un PDF est rasterisé à 300 dpi par pypdfium2 dans une tâche.
2. **Cadre de page** (`page_frame`) : feuille détectée, puis perspective, rotation, rognage et redressement. Toutes les coordonnées de zones sont des fractions de ce cadre.
3. **OCR** (`layout.read_text`) : copie grise « papier blanchi », puis `tesseract … --psm 3 --dpi 300 tsv`. Si la confiance moyenne est inférieure à 75, une seconde lecture binarisée (Sauvola) est faite et la meilleure est gardée.
4. **Illustrations** : zones `image` détectées sur la page **tonale**, découpées sur une version couleur et enregistrées en PNG sans perte (`SourceImage`, `ImageOccurrence` à la même place). Le texte situé à plus de 50 % dans une image est jeté.
5. **PDF B** : `sheet_geometry` (A4 avec marge, ou « original » à 300 dpi), conversion des fractions en points avec inversion de l'axe Y (ReportLab a son origine en bas à gauche).
6. **Validation** : relecture par pdfminer.

## 2. Fonctionnalités : implémentées, incomplètes, problèmes

| # | Type | Constat |
|---|---|---|
| 1 | Bug [V] | `requirements.txt` ne déclare ni **reportlab** ni **pypdfium2**, alors que `rebuild.py`, `fonts.py`, `validate.py` et `tasks.py` les importent. Une installation neuve échoue à l'export reconstruit et à l'import PDF. Ils ne sont présents dans `.venv` que par une installation manuelle. |
| 2 | Limite [V] | En format « original », la taille de page suppose 300 dpi (`k = 72/300`). Une photo de 5568 px donne une page de 1336 pt (47 cm). La taille physique n'est ni estimée ni réglable. |
| 3 | Limite [V] | Les tableaux et zones « autre » sont collés **en image** dans le PDF B : leur texte n'y est pas modifiable. |
| 4 | Limite [V] | Taille de police estimée par `hauteur de ligne / 1.15` : calibration unique, non vérifiée par test. |
| 5 | Risque [V] | `PAGE_NUMBER` accepte tout mot composé de lettres romaines (« mix », « civil », « dim ») dans les bandes haute et basse : un court en-tête peut être classé « numéro de page ». |
| 6 | Coût [V] | L'orientation automatique peut lancer Tesseract jusqu'à 5 fois par page (OSD, puis 4 lectures). |
| 7 | Limite [V] | Mode A dépendant du binaire `ocrmypdf` (et de Ghostscript) en plus de Tesseract. |
| 8 | Limite [V] | Aucun titre ou sous-titre distingué : une seule catégorie « heading ». |
| 9 | Risque [V] | Une légende chevauchant une illustration à plus de 50 % est jetée avec le « texte dans l'image ». |
| 10 | Sécurité [V] | Un fichier `.env ` (avec espace) contenant un secret Django existe dans l'arbre ; il est désormais ignoré par `.gitignore`. Hors périmètre de la nouvelle application (sans comptes ni secrets). |
| 11 | Architecture [V] | Logique très liée à l'ORM (fonctions qui prennent `book`, `page` Django) : non réutilisable telle quelle hors Django. |
| 12 | Qualité [H] | Les heuristiques (seuils de `picture_mask`, `HEADING_RATIO`…) semblent réglées sur un seul corpus (photos sur fond sombre) : à reconfirmer sur d'autres livres. |

## 3. Ce qui est repris, corrigé ou abandonné

| Élément de l'ancien projet | Décision dans la nouvelle application |
|---|---|
| Coordonnées en fractions 0..1 du cadre de page | **Repris** : c'est le repère unique image ↔ aperçu ↔ éditeur ↔ PDF |
| Séparation `ocr_text` et `corrected_text` | **Repris** : une correction n'est jamais écrasée et l'original OCR reste consultable |
| Détection de la feuille, perspective, projection pour l'inclinaison, Sauvola | **Repris et réécrit** en fonctions pures (tableaux NumPy en entrée et en sortie), testées sur des images synthétiques |
| Lecture TSV de Tesseract en sous-processus, seconde lecture binarisée si la confiance est faible | **Repris** ; erreurs explicites (binaire absent, langue absente) |
| OSD contrôlé par lecture | **Simplifié** : OSD seul, appliqué au-delà d'un seuil de confiance ; sinon page laissée telle quelle et signalée |
| Masque « image » Lab, réglures de tableaux, règles de classification | **Repris et étendu** : titre ou sous-titre distingués, numéro de page limité aux chiffres et aux romains courts, légende conservée même si elle chevauche l'image |
| Estimation alignement, gras, couleur | **Reprise** ; taille de police **calibrée par test** |
| Rendu ReportLab : réduction, extension vers le bas, signalement | **Repris** avec une règle explicite et documentée |
| Page blanche signalée si non analysée (jamais le scan en fond en mode B) | **Repris** |
| Mode A | **Réécrit sans OCRmyPDF** : image de page et couche de texte invisible issue du texte OCR **corrigé** (une dépendance système en moins) |
| Tableaux collés en image | **Corrigé** : texte OCR rendu en texte réel, la zone étant marquée « tableau » et signalée pour vérification |
| Taille de page « 300 dpi » | **Corrigée** : formats A4, A5, Letter ou personnalisé, ou taille calculée à partir d'une résolution choisie |
| Comptes, propriétaires, Docker, CI, worker, PostgreSQL, TDM cliquable, profils couleur | **Abandonnés** (hors périmètre ou repoussés ; voir ROADMAP) |
| Éditeur SVG `boxes.js` | **Concept repris** dans un composant Streamlit v2 (SVG, déplacement et redimensionnement à la souris) |

## 4. Choix techniques de la nouvelle application

- **Streamlit 1.66**, Python 3.14 (testé) ; compatible 3.12+ [H : 3.12 non testé].
- **OCR : Tesseract 5** en sous-processus (sortie TSV). Comparaison :
  - Tesseract : paquet Linux standard, léger, `fra` et `eng` installés ici, mots, lignes, paragraphes, boîtes et confiance en une passe. **Retenu.**
  - PaddleOCR : meilleur sur les photos difficiles [H], mais PaddlePaddle pèse plusieurs centaines de Mo, ses roues pour Python 3.14 ne sont pas garanties et il ne fournit pas la structure paragraphe et ligne de Tesseract. **Non retenu, non testé.**
  - EasyOCR : dépend de PyTorch (> 1 Go). **Non retenu, non testé.**
  - `pytesseract` : simple enveloppe du même binaire, sans gain fonctionnel ; un appel direct permet de maîtriser le délai et les messages d'erreur. **Non utilisé.**
- **PDF : ReportLab** (licence BSD), texte vectoriel, polices TTF intégrées en sous-ensemble, Unicode. **pypdfium2** (Apache/BSD) sert à rasteriser l'aperçu (l'aperçu **est** le PDF), à importer les PDF et à relire le PDF pour la validation (texte, images, taille). PyMuPDF est écarté pour sa licence AGPL, comme dans l'ancien projet.
- **Modèles : Pydantic v2** (validation stricte, JSON versionné).
- **Persistance** : un dossier par projet dans `workspace/projects/<id>/`, contenant `project.json`, les originaux, les images de travail, les illustrations et les exports.

## 5. Compléments demandés par l'extension du cahier des charges

### 5.1 Outils de correction d'image de l'ancien projet [V]

| Où | Ce qui existe | Limite constatée |
|---|---|---|
| `Book` (réglages **globaux au livre**) | luminosité, contraste (PIL `ImageEnhance`), CLAHE (L de Lab, noirs protégés), nettoyage du fond (division par le fond estimé par canal, illustrations protégées par `picture_mask`), atténuation du verso (courbe de niveaux), débruitage (médian ou bilatéral), netteté (masque flou), sortie couleur, gris ou N&B (Sauvola) | Pas de réglage par page : un second jeu `photo_*` s'applique aux pages marquées « avec photo ». Pas de saturation, teinte, température, gamma, exposition, ombres ou hautes lumières, ni de balance des blancs manuelle. |
| `color.py` | 6 profils (original, restauration douce, texte noir, livre ancien, illustrations, N&B) : dominante mesurée sur le papier et sur le texte en Lab, retirée avec une intensité réglable ; illustrations moins corrigées | Profils prédéfinis, sans curseurs individuels. |
| `images.py` (`SourceImage.edits`) | Par illustration : recadrage, quart de tour, angle fin, miroirs, luminosité, contraste, saturation, netteté, débruitage, redimensionnement ; **paramètres stockés et réappliqués depuis la source PNG** (non destructif) | Pas de balance des blancs ni de courbes ; aucune correction locale dans la page. |
| `image_processing.py` | Rognage manuel des 4 bords (en %, **global au livre**), quad manuel par page (`crop_quad`), angle manuel (`skew_override`), signature de géométrie (`Geometry.signature`) qui rend les zones OCR obsolètes | Aucune correction locale rectangulaire ; pas de distinction explicite « image pour l'OCR » et « image pour l'impression » (l'OCR lit l'image tonale de la page). |

**Repris** : paramètres non destructifs réappliqués depuis la source, CLAHE sur L avec protection des noirs, protection des illustrations lors du nettoyage du fond, Sauvola, signature de géométrie. **Ajouté** : réglages par page en deux jeux distincts (impression et OCR), corrections locales rectangulaires, modes A, B et C, brouillon et validation explicite des réglages d'illustration, historique annuler et rétablir.

### 5.2 Gestion et numérotation des pages de l'ancien projet [V]

- Ordre stocké dans `PageImage.order` (unique par livre) ; `services.reorder_pages` renumérote en deux passes à cause de la contrainte d'unicité ; `renumber` comble les trous après une suppression.
- Doublons refusés par SHA-256 du fichier ; un PDF importé est rasterisé à 300 dpi en PNG, une page par page.
- Les numéros **imprimés** sont lus seulement pour la table des matières (`toc.py` : bande haute et basse, confiance ≥ 70, cohérence croissante).
- Nouvelle application : ordre = position dans `Project.pages` (liste, sans contrainte d'unicité à contourner), doublons ignorés par couple (SHA-256, index de page), numéro imprimé détecté par l'analyse de mise en page et stocké dans `Page.printed_page_number` (affiché, non utilisé pour réordonner automatiquement).

## 6. Hypothèses de périmètre

- Mono-utilisateur, local, sans authentification ; Streamlit est lancé sur la machine de l'utilisateur.
- Les pages sont traitées une par une (mémoire bornée) ; aucun parallélisme dans la première version.
- La reconstruction est fidèle **aux blocs détectés**, pas garantie fidèle au livre : police, taille et style sont estimés et restent modifiables.
- Les photos réelles de l'ancien projet (`media/books/2026/10/pages/`) servent au test de bout en bout, **copiées** dans le dossier de travail du test, jamais modifiées.
