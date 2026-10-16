# Feuille de route

## Version 1 (en cours) : parcours complet local

| Phase | Contenu | Responsable |
|---|---|---|
| 1 | Audit de l'ancien projet, documentation, contrats (modèles, coordonnées, erreurs, pages synthétiques) | Coordinateur (agents 1 et 2) |
| 2 | Import multi-fichiers (JPG, PNG, TIFF multi-pages, PDF), stockage par projet, miniatures, ordre des pages | Coordinateur (services) |
| 3 | Prétraitement : géométrie (orientation, feuille, perspective, inclinaison, marges, échelle), réglages photométriques non destructifs, corrections locales, modes texte, illustration et mixte | Agent 3, vision |
| 4 | OCR Tesseract (TSV), seconde lecture binarisée, orientation OSD, langues installées | Agent 4, OCR |
| 5 | Analyse de mise en page : types de blocs, colonnes, ordre de lecture, styles estimés, faible confiance | Agent 4, OCR |
| 6 | Extraction des illustrations (photos, dessins, tableaux réglés), légendes associées | Agent 4, OCR |
| 7 | Modèles typés et JSON versionné, sauvegarde et reprise, annuler et rétablir | Coordinateur |
| 8 | Moteur PDF ReportLab : mode B reconstruit, mode A fac-similé, polices intégrées, débordements | Agent 5, PDF |
| 9 | Interface Streamlit : import, traitement, éditeur d'images, éditeur de blocs à la souris, export | Agent 6, interface |
| 10 | Export et validation du PDF (relecture pypdfium2) | Agents 5 et 1 |
| 11 | Tests d'intégration, rapport de tests, parcours réel sur les photos de l'ancien projet | Agent 7, qualité, puis coordinateur |

## Versions suivantes (hors périmètre de la V1)

- Table des matières cliquable et signets (concept de `toc.py` de l'ancien projet).
- Profils de restauration des couleurs par page (concept de `color.py`).
- Sélection libre (lasso) pour les corrections locales ; la V1 propose le rectangle.
- Traitement parallèle des pages (processus séparés) pour les gros livres.
- Conteneur Docker (l'architecture n'en dépend pas : chemins configurables par variables d'environnement).
- Moteur OCR alternatif enfichable (PaddleOCR) derrière l'interface de `core/ocr_engine.py`.
