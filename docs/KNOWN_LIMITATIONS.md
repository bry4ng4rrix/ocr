# Limitations connues

Propriétaire : `integration-documentation`. Une limite est **constatée** (preuve : test ou mesure, citée) ou **attendue** (à confirmer par un test). Mise à jour à chaque phase.

## Environnement (constaté le 16/10/2026)

| Limite | Effet | Contournement |
|---|---|---|
| PaddlePaddle n'a aucune roue pour Python 3.14 | PaddleOCR est inutilisable dans cet environnement | Tesseract (D-02). PaddleOCR possible plus tard dans un venv Python 3.12 |
| Langue `deu` absente (installées : `afr`, `eng`, `fra`, `osd`) | OCR allemand impossible, alors que **les photos réelles disponibles sont en allemand** | `sudo pacman -S tesseract-data-deu` |
| LibreOffice absent | Pas de conversion DOCX → PDF ni de contrôle du rendu DOCX | `sudo pacman -S libreoffice-fresh`. Le PDF direct (PyMuPDF) reste disponible |
| ~~Paquet obsolète `docx` 0.2.4 dans `.venv`~~ | Corrigé le 16/10/2026 : désinstallé, python-docx 1.2.0 réinstallé, `pip check` sans erreur | — |
| OSD Tesseract sur la photo brute du banc | Échec (« Too few characters ») : la feuille est trop petite dans le cadre | Isoler la feuille **avant** l'OSD (ordre imposé dans `orientation_service`) |

## OCR (attendu)

- Tesseract est moins robuste que les OCR neuronaux sur les photos floues, très inclinées ou mal éclairées. La confiance est affichée et les mots faibles sont signalés.
- L'OSD (orientation) échoue sur les pages presque sans texte. La page reste alors à 0° avec un avertissement, et la rotation manuelle reste possible.
- Le texte sur fond photographique, les lettrines, les petites capitales et les polices décoratives sont souvent mal lus.
- Les tableaux sont reconnus comme du texte aligné. La structure en cellules n'est reconstruite que pour les tableaux réglés (attendu, à confirmer).

## Correction géométrique (attendu)

- La courbure n'est corrigée que si elle est mesurable de façon fiable (D-11). Une page très bombée près de la reliure reste déformée.
- La perspective n'est corrigée que si les quatre bords de la feuille sont visibles.
- Les pixels de fond cachés par le texte imprimé ne sont **pas** retrouvés. Le fond « nettoyé » est une estimation (inpainting), signalée comme telle, et l'image source est conservée.

## Mise en page (attendu)

- La police, la taille, le gras et l'italique sont **estimés**. La typographie exacte du livre n'est pas identifiée, mais tout reste modifiable.
- Le format physique de la page est estimé à partir du ratio et de la taille du corps de texte. Il faut le vérifier.

## DOCX : Word et LibreOffice (attendu, spike T-30)

- Les cadres (`w:framePr`) et les images ancrées sont rendus avec de légers décalages selon le logiciel. Le mode `fidelity` est le plus sensible.
- Un cadre de hauteur exacte coupe le texte ajouté par l'utilisateur. La hauteur « au moins » fait déborder sur les blocs voisins. Le rapport d'export signale les blocs concernés.
- Les polices absentes de la machine du lecteur sont remplacées par Word ou LibreOffice. Le PDF, lui, intègre les polices.

## PDF

- PyMuPDF est sous licence AGPL-3.0. C'est sans conséquence en usage local, mais une contrainte en cas de redistribution (D-04).
