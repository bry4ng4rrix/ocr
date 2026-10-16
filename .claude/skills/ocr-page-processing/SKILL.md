---
name: ocr-page-processing
description: Analyser une photo de page de livre - orientation (0/90/180/270), inclinaison, contour de feuille, OCR Tesseract avec coordonnées et confiance, zones de faible confiance. À utiliser pour implémenter ou déboguer orientation_service, image_service ou ocr_service, ou pour diagnostiquer pourquoi une page est mal lue.
---

# Traitement OCR d'une page

## Diagnostic rapide d'une image (fonctionne sans le code de l'application)

```bash
.venv/bin/python ${CLAUDE_SKILL_DIR}/scripts/ocr_probe.py chemin/page.jpg --lang fra+eng
```

Le script ne modifie jamais l'image : il travaille sur une copie en mémoire et affiche (JSON) l'OSD Tesseract (rotation, confiance, script), la confiance moyenne, le nombre de mots, de lignes et de paragraphes, les mots de confiance < 60 et le temps de chaque étape. `--save-debug DIR` écrit une image annotée (boîtes colorées par confiance) dans `DIR`, jamais à côté de l'original.

## Ordre des corrections (contrat de `orientation_service`)

1. EXIF (orientation de l'appareil photo).
2. Contour de feuille → perspective, **seulement** si le quadrilatère couvre 35–98 % de l'image et ses angles sont proches de 90° après redressement ; sinon on garde l'image entière et on avertit.
3. Rotation par quart de tour : OSD Tesseract (`--psm 0`) sur une copie réduite (~1600 px). Appliquée si `orientation_conf ≥ 2` ; entre 0,5 et 2 → contrôle par lecture (`--psm 6`, score = confiance moyenne × nb de mots) sur l'orientation OSD et sur 0° ; sinon 0° + avertissement « orientation incertaine ».
4. Inclinaison fine (±10°) par profil de projection des pixels d'encre ; ignorée si |angle| < 0,15° ou si la courbe de score est plate (incertain).
5. Courbure : seulement si les lignes de texte détectées sont nombreuses et courbées de façon cohérente ; jamais sur une page à grande illustration.

Une page nette et droite doit ressortir quasi identique : vérifier `mean(abs(out - in))` dans les tests.

Photos du banc de numérisation (corpus réel) : la feuille occupe environ 40 % du cadre, sur fond noir ; la page suivante est visible sous les reflets de LED. L'OSD sur l'image entière échoue : isoler la plus grande feuille claire **avant** l'étape 3.

## OCR Tesseract

- Commande : `tesseract img.png stdout -l fra+eng --psm 3 --dpi 300 tsv` (sous-processus avec délai ; `pytesseract` n'apporte rien).
- Colonnes TSV : `level page_num block_num par_num line_num word_num left top width height conf text`. `conf = -1` pour les niveaux non-mot.
- Image envoyée : niveaux de gris, éclairage égalisé (division par le fond estimé), **pas** de binarisation par défaut ; seconde lecture binarisée (Sauvola) si confiance moyenne < 60, la meilleure est gardée.
- Langues installées : `tesseract --list-langs`. Langue manquante → erreur explicite avec la commande d'installation (`sudo pacman -S tesseract-data-deu`, `sudo apt install tesseract-ocr-deu`).
- Cache : clé = SHA-256(image OCR PNG) + langues + psm + version Tesseract.

## Pièges connus

- L'OSD échoue (« Too few characters ») sur les pages presque sans texte : traiter `None` comme « incertain », pas comme 0° sûr.
- L'OSD renvoie `Rotate: 90` = rotation **horaire** à appliquer pour redresser ; vérifier le sens sur un test synthétique avant tout changement.
- Tesseract coupe les lettrines et lit le texte sur fond photo avec une confiance faible : signaler, ne pas jeter.
- Les petites capitales et chiffres romains en en-tête sont souvent mal classés : laisser la décision à `layout_service`.
