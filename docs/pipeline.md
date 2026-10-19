# Pipeline

Une page passe par des fichiers **séparés**, régénérables depuis l’original et les paramètres.

1. **Import** — copies dans `originals/` (SHA-256) ; jamais réécrites. Import navigateur (mémoire) ou dossier disque (un fichier à la fois).
2. **Géométrie** — contour, perspective, orientation, inclinaison, courbure prudente → `corrected/`.
3. **Amélioration** — optionnelle → `enhanced/` ; originaux intacts.
4. **OCR** — Tesseract TSV → `ocr/<page>.json` ; l’éventuel résultat précédent est copié dans `ocr/history/`.
5. **Mise en page** — blocs, illustrations, fonds → `images/`, `backgrounds/`.
6. **Routage** — bandes A–E à partir de mesures (confiance, vide, fichiers manquants). Aucun appel Ollama/Gemini automatique.
7. **Composition** — positions en points (source unique de l’aperçu et des exports).
8. **Export** — DOCX (`fidelity` / `editable` / `hybrid`) et PDF PyMuPDF ; conversion LibreOffice si `soffice` est présent.

Reprise : `job.json` + enregistrement après chaque page. Un redémarrage marque le job `interrupted` ; relancer reprend les pages non terminées.
