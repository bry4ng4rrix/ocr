---
name: pdf-export-validation
description: Produire ou convertir un PDF (rendu direct PyMuPDF, fac-similé recherchable, conversion DOCX vers PDF par LibreOffice headless) et vérifier le résultat - nombre et taille des pages, texte extractible, images, pages blanches, polices intégrées, débordements. À utiliser pour pdf_service ou pour contrôler un .pdf exporté.
---

# Export PDF et validation

## Contrôler un PDF existant

```bash
.venv/bin/python ${CLAUDE_SKILL_DIR}/scripts/check_pdf.py livre.pdf [--expect-pages N] [--expect-text "mot" ...] [--render DOSSIER]
```

Sortie JSON par page : taille en mm, caractères extractibles, nombre d'images, polices (intégrées ou non), page visuellement blanche (rendu à 40 dpi, < 0,2 % de pixels non blancs). `--render DOSSIER` écrit un PNG par page pour inspection (jamais dans `data/projects/*/originals`). Code 1 si une attente échoue.

## Conversion DOCX → PDF (LibreOffice)

```bash
soffice --headless --norestore -env:UserInstallation=file:///tmp/lo_profile_$$ \
        --convert-to pdf --outdir SORTIE fichier.docx
```

- Profil utilisateur temporaire (`-env:UserInstallation`) : évite le blocage si une instance LibreOffice tourne déjà.
- Délai maximal (sous-processus avec `timeout`), puis vérifier que `SORTIE/fichier.pdf` existe **et** passe `check_pdf.py`.
- Comparer le nombre de pages au nombre de pages source ; un écart = débordement de cadre ou saut de page perdu : le signaler, ne pas le corriger en supprimant des sauts.
- `soffice` absent : la fonction renvoie un résultat « indisponible » explicite ; les tests sont `skip` avec la raison.

## Rendu direct PyMuPDF (PDF « fidèle à la reconstruction »)

- `page = doc.new_page(width=pt, height=pt)` par page source ; fond et illustrations via `page.insert_image(rect, filename=..., keep_proportion=True)`.
- Texte : `page.insert_htmlbox(rect, html, css=..., archive=fitz.Archive(dossier_polices))` → renvoie `(spare_height, scale)` ; `spare_height < 0` ou `scale < 1` = débordement à signaler (et réduction éventuelle selon la politique choisie).
- Polices : `@font-face { font-family: X; src: url(fichier.ttf); }` dans le CSS, fichiers servis par l'archive ; vérifier qu'elles apparaissent intégrées dans `page.get_fonts()`.
- Fac-similé recherchable : image de page corrigée + texte invisible (`render_mode=3`) positionné sur les boîtes OCR corrigées.

## Critères de validation

Nombre de pages exact ; texte extractible sur chaque page reconstruite ; images attendues présentes ; aucune page blanche involontaire ; polices intégrées ; débordements listés. Un PDF qui « s'ouvre » n'est pas validé pour autant.
