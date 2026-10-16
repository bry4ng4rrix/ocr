---
name: debug-ocr
description: Diagnostiquer un problème OCR sur une page (orientation, confiance, langue, prétraitement) à partir d'une image ou d'une page de projet.
disable-model-invocation: true
argument-hint: "<image | id_projet numéro_page> [langues, ex. fra+eng]"
---

# Diagnostic OCR

Arguments : `$ARGUMENTS`

- Langues installées : !`tesseract --list-langs 2>&1 | tail -n +2 | tr '\n' ' '`

## Étapes

1. Trouver l'image :
   - chemin d'image → l'utiliser tel quel ;
   - `id_projet numéro_page` → lire `data/projects/<id>/project.json`, prendre la page à ce rang, et diagnostiquer **à la fois** l'original (`originals/`) et l'image corrigée (`corrected/`) pour séparer un défaut de prétraitement d'un défaut d'OCR.
2. Vérifier que les langues demandées sont installées ; sinon donner la commande d'installation (Arch : `sudo pacman -S tesseract-data-deu` ; Debian : `sudo apt install tesseract-ocr-deu`).
3. Lancer `.venv/bin/python .claude/skills/ocr-page-processing/scripts/ocr_probe.py IMAGE --lang LANGUES --save-debug <scratchpad>` puis regarder l'image annotée (Read).
4. Interpréter avec la section « Pièges connus » du skill `ocr-page-processing` : orientation, inclinaison, contraste, langue, texte sur fond, colonnes fusionnées.
5. Compte rendu : symptôme, cause la plus probable avec la preuve (valeurs OSD, confiance, image annotée), correction proposée (réglage utilisateur ou changement de code et son propriétaire). Ne modifier aucun fichier du projet.
