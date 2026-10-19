# Décisions techniques

Format : contexte → options → décision → conséquences. États : `PROPOSÉE`, `RETENUE`, `À VÉRIFIER` (spike prévu dans planning.md), `REMPLACÉE`.

## D-01 — Repartir d'une nouvelle structure en réutilisant les algorithmes de l'ancien code — RETENUE

- **Contexte** : le dépôt contenait une application Streamlit de reconstruction **PDF uniquement** (≈ 11 000 lignes, commit `ef66f25`). L'utilisateur l'a supprimée volontairement (commit `7760065 suppressions`). Le nouveau cahier des charges impose DOCX, PyMuPDF, trois modes de reconstruction et une architecture `services/*_service.py`, `models/` et `components/`.
- **Options** : (a) restaurer l'ancien code et l'adapter ; (b) nouvelle structure, en reprenant les algorithmes déjà testés ; (c) tout réécrire sans regarder l'ancien code.
- **Décision** : (b). L'ancien code reste consultable par `git show ef66f25:<fichier>` et sert de référence pour : détection de feuille et perspective (`core/image_preprocessing.py`), inclinaison par profil de projection, Sauvola, appel TSV de Tesseract et seconde lecture binarisée (`core/ocr_engine.py`), découpe des lignes et classification (`core/layout_analysis.py`), masque d'illustrations Lab (`core/illustration_extraction.py`), réglages non destructifs (`core/image_adjustments.py`), éditeur de boîtes SVG (`ui/box_editor.py`).
- **Conséquences** : la suppression faite par l'utilisateur est respectée et rien n'est perdu. Chaque reprise est retestée dans la nouvelle structure. ReportLab et pypdfium2 sont abandonnés au profit de PyMuPDF (D-04).

## D-02 — Moteur OCR : Tesseract 5 — RETENUE

- **Contexte** : le cahier des charges autorise PaddleOCR ou Tesseract. Environnement : Python 3.14.8 et Arch Linux.
- **Constat vérifié** (16/10/2026) : `pip install --dry-run paddlepaddle` donne « No matching distribution » : PaddlePaddle ne publie aucune roue pour Python 3.14, donc PaddleOCR 3.7 ne peut pas fonctionner ici. Tesseract 5.5.3 est installé avec `eng`, `fra`, `afr` et `osd`.
- **Décision** : Tesseract en sous-processus (sortie TSV : mots, lignes, paragraphes, boîtes et confiance en une passe ; OSD pour l'orientation). Interface `OcrEngine` (Protocol) pour brancher PaddleOCR plus tard, dans un environnement Python 3.12.
- **Conséquences** : l'allemand demande `sudo pacman -S tesseract-data-deu` (action de l'utilisateur, tâche BLOCKED). La qualité est moindre que PaddleOCR sur les photos difficiles : c'est une limite documentée.

## D-03 — Détection de mise en page : segmentation Tesseract et heuristiques OpenCV — RETENUE (révisable)

- **Options** : (a) segmentation de Tesseract (blocs, paragraphes, lignes) et règles OpenCV pour les illustrations, fonds, colonnes et types ; (b) modèle d'apprentissage profond (DocLayout-YOLO, PP-Structure, LayoutParser/Detectron2) ; (c) les deux.
- **Analyse** : (b) demande PyTorch ou Paddle (plusieurs centaines de Mo à plus de 1 Go), des poids téléchargés et un GPU de préférence ; PP-Structure est indisponible (D-02). La machine a 4 cœurs et 11 Go de RAM.
- **Décision** : (a) derrière une interface `LayoutDetector`. Un détecteur appris pourra être ajouté plus tard sans changer les contrats.
- **Conséquences** : les règles sont réglées sur des pages synthétiques et sur les 11 photos réelles disponibles. Les erreurs de classification sont corrigibles dans l'éditeur.

## D-04 — PDF : PyMuPDF pour le rendu, l'aperçu, l'import et la validation — RETENUE

- **Contexte** : PyMuPDF est imposé ; pypdfium2 et ReportLab feraient doublon.
- **Constat** : PyMuPDF 1.28.2 s'installe sous Python 3.14 (roue abi3).
- **Décision** : PyMuPDF seul : rasterisation des PDF importés, rendu direct des pages composées (`insert_htmlbox` : texte réel, CSS, polices TTF intégrées, mesure du débordement), fac-similé recherchable (texte invisible), aperçu et relecture.
- **Conséquences** : PyMuPDF est sous licence **AGPL-3.0** (ou licence commerciale). Pour un usage local, aucun problème. Une redistribution de l'application en service réseau imposerait l'AGPL. À signaler à l'utilisateur.

## D-05 — DOCX : python-docx et XML OOXML pour ce qu'il ne gère pas — RETENUE

- python-docx 1.2.0 gère les sections, les styles, les images inline, les en-têtes et les pieds. Les ancres flottantes (`wp:anchor`), les cadres (`w:framePr`) et les colonnes (`w:cols`) sont ajoutés en XML, dans des fonctions isolées et testées.
- Le paquet obsolète **`docx` 0.2.4** (module `docx.py`, Python 2) est installé dans `.venv` en plus de python-docx : il faut le désinstaller, puis réinstaller python-docx (tâche T-02).

## D-06 — Trois modes de reconstruction — RETENUE

| Mode | Texte | Images et illustrations | Fond graphique (cas B) | Compromis |
|---|---|---|---|---|
| `fidelity` (fidélité) | **positionné en absolu** (cadres) à la place et à la taille détectées | ancrées en absolu | image de fond derrière le texte, texte d'origine effacé | Apparence la plus proche. Éditer un long passage peut faire déborder un cadre (signalé). Le rendu diffère un peu entre Word et LibreOffice. |
| `editable` (modifiable) | **flux** de paragraphes stylés dans l'ordre de lecture ; colonnes par sections ; espacements estimés | inline, à leur place dans le flux, taille d'origine | conservé comme image ancrée derrière le texte (option ; défaut activé) | Plus facile à éditer et à restyler. Les positions exactes sont perdues : le texte reflue. |
| `hybrid` (hybride) | **flux** pour le texte courant ; **cadres** pour le texte posé sur un fond ou une image | ancrées en absolu (habillage carré) | fond ancré derrière | Compromis : composition visuelle conservée, texte courant éditable. Plus complexe : certains habillages diffèrent selon le logiciel. |

Règles communes : une page source = une section DOCX = une page PDF (mode page par page) ; aucun mode ne remplace du texte par une image ; le cas A n'a jamais de fond photographique.

## D-07 — Positionnement absolu dans Word : cadres `w:framePr` ou zones de texte DrawingML — RETENUE (cadres)

- **Options** : (a) paragraphe dans un cadre (`w:framePr`) : simple, du vrai texte de paragraphe, relu par LibreOffice et Word ; (b) zone de texte DrawingML (`wps:wsp` dans `mc:AlternateContent` avec repli VML) : plus riche, mais XML lourd et rendu LibreOffice variable.
- **Spike T-30 (16/10/2026, sans LibreOffice)** : les deux variantes sont des DOCX valides relus par python-docx. Avec un cadre, le texte est un vrai paragraphe du corps (vu par `document.paragraphs` et par `check_docx.py`). Avec une zone de texte `wps`, le texte est **invisible** pour python-docx, il est dupliqué (DrawingML + repli VML) et la validation échoue.
- **Décision** : (a), cadres `w:framePr` (`hAnchor/vAnchor="page"`, hauteur `atLeast` pour ne jamais masquer un texte allongé, `wrap="none"` en fidélité, `around` en hybride). État : **RETENUE** ; la comparaison de rendu Word/LibreOffice reste à faire quand LibreOffice sera installé (T-53).

## D-08 — Une seule composition pour l'aperçu et les exports — RETENUE

`composition_service.compose_page` calcule toutes les positions en points. L'aperçu affiché est le **rendu PDF rasterisé** de cette composition, donc ce qui est vu est ce qui est exporté. Le DOCX part de la même composition. Ses écarts de rendu (moteur Word ou LibreOffice) sont contrôlés par la conversion LibreOffice quand elle est disponible.

## D-09 — Persistance : dossier par projet et JSON versionné — RETENUE

`data/projects/<id>/project.json` (Pydantic, `schema_version`, écriture atomique avec `.bak`). Fichiers binaires à côté. L'historique par page est fait d'instantanés JSON bornés. La sauvegarde est explicite (bouton) ou automatique (délai configurable, 0 = désactivée). Pas de base de données : le projet est mono-utilisateur, local, et le dossier se sauvegarde d'un bloc.

## D-10 — Amélioration d'image : OpenCV et NumPy, sans bibliothèque supplémentaire — RETENUE

- **Options** : scikit-image, Pillow `ImageEnhance`, OpenCV.
- **Décision** : OpenCV (CLAHE sur L de Lab, `fastNlMeansDenoisingColored`, `bilateralFilter`, masque flou, `inpaint`) et NumPy (balance des blancs, gamma). Pillow sert aux entrées et sorties. scikit-image ferait doublon.

## D-11 — Courbure des pages : correction prudente, désactivable — PROPOSÉE

Modèle cylindrique estimé à partir des lignes de texte (ajustement polynomial des lignes de base, interpolé verticalement). Appliqué seulement si au moins 8 lignes sont cohérentes, si le résidu baisse nettement et si la page ne contient aucune grande illustration. Sinon : non appliqué, avec un avertissement. L'option peut être désactivée par page.

## D-12 — Traitement en arrière-plan : thread et état sur disque — PROPOSÉE

Un thread par projet (pas de Celery ni de file externe). L'état est écrit dans `job.json`, et le projet est enregistré après chaque page. Un redémarrage interrompt le lot, qui reprend sur les pages non traitées.

## D-13 — Organisation Claude Code — RETENUE

- Version : Claude Code 2.1.301 (extension VS Code ; le CLI `claude` n'est pas dans le `PATH`). Formats vérifiés dans la documentation officielle (code.claude.com/docs : sub-agents, skills, memory, permissions) le 16/10/2026.
- Règles : `AGENTS.md` est la source unique, importée par `CLAUDE.md` (`@AGENTS.md`), méthode recommandée par la documentation. Il n'y a donc pas de fichiers contradictoires.
- Commandes : des skills avec `disable-model-invocation: true` (`.claude/commands/` est encore pris en charge, mais la documentation recommande les skills).
- Le skill de revue s'appelle **`project-code-review`** et non `code-review`. D'après la documentation, un skill de projet `code-review` **remplace** la commande intégrée `/code-review`. L'utilisateur la perdrait.
- Sous-agents : outils `Read, Grep, Glob, Bash, Edit, Write`, sans `Agent` (pas de délégation en cascade : l'agent principal coordonne). Les skills utiles sont préchargés par le champ `skills`.
- Aucune règle de permission n'a été ajoutée à `.claude/settings.json` : les permissions de l'utilisateur ne sont pas élargies sans demande.

## D-14 — Convention d'interligne et d'espacement — RETENUE

`ResolvedStyle.line_spacing` est un **multiple de l'interligne simple de la police** (convention Word). Le PDF le multiplie par l'interligne naturel de la police (≈ 1,149 pour Liberation Serif). Les espacements avant/après ne sont pas réappliqués dans les boîtes du PDF, puisque les positions les contiennent déjà. Dans le DOCX en flux, l'espace avant vaut `gap_before_pt` et l'espace après vaut 0 (Word additionne les deux).

## D-15 — Débordement de texte — RETENUE

En PDF, avec la politique `shrink`, la police est réduite par pas de 3 % jusqu'à 70 %. Au-delà, la boîte est prolongée vers le bas et le bloc est signalé. Avec la politique `report`, le bloc est seulement signalé. Le texte n'est jamais perdu. En DOCX, les cadres ont une hauteur « au moins » : un texte allongé reste visible mais peut chevaucher un bloc voisin.

## D-16 — Numérisation intelligente : Gemma propose, les mesures décident — RETENUE

- **Contexte** : le client veut un PDF fidèle (photographies traitées et OCR invisible) par livre ; Gemma 3:4b est installé localement (Ollama 0.40.6, exécuté sur CPU).
- **Constats** (19/10/2026, pages réelles) : 63 à 117 s par analyse ; réponses souvent incohérentes (orientation, actions, type de page). Le schéma JSON imposé par `format` est respecté : la forme est fiable, le contenu ne l'est pas.
- **Options** : (a) appliquer les recommandations de Gemma ; (b) ignorer Gemma ; (c) recommandations en liste blanche, retenues seulement si une mesure OpenCV les confirme (seuils plus souples que sans IA).
- **Décision** : (c). La géométrie (coins, rotation) reste à OpenCV et Tesseract ; Gemma ne déclenche qu'une vérification. Par défaut (`OLLAMA_AUTO_APPLY=false`), le plan mesuré est appliqué et le plan combiné est proposé après aperçu.
- **Autres choix** :
  - module séparé de la reconstruction DOCX, qui réutilise projet, géométrie, OCR et SQLite ;
  - PDF construit avec PyMuPDF, mot par mot (pas d'OCRmyPDF : absent, et il refait l'OCR et réencode) ;
  - image finale en JPEG unique, insérée telle quelle ;
  - file d'états dans SQLite (`scan_tasks`) et détails par page en JSON ;
  - un seul worker de numérisation par processus.
- **Conséquences** :
  - le module fonctionne entièrement sans IA ;
  - l'analyse Gemma est trop lente sur ce poste pour 8 400 pages : à réserver aux pages douteuses, ou à exécuter sur un serveur avec GPU.
