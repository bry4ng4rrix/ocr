---
name: docx-export-validation
description: Produire un DOCX modifiable avec python-docx (styles réutilisables, sections aux dimensions de chaque page, sauts de page, images inline ou ancrées, fonds, cadres positionnés) et vérifier son intégrité et son rendu. À utiliser pour document_service ou pour contrôler un .docx exporté.
---

# Export DOCX et validation

## Contrôler un DOCX existant

```bash
.venv/bin/python ${CLAUDE_SKILL_DIR}/scripts/check_docx.py export.docx [--expect-pages N] [--expect-images N] [--expect-text "mot1" "mot2"]
```

Sortie JSON : validité du ZIP, ouverture python-docx, nombre de sections, sauts de page explicites, paragraphes non vides par style, images dans `word/media`, ancres flottantes (`wp:anchor`) et cadres (`w:framePr`), textes attendus manquants, polices utilisées. Code de sortie 1 si une attente n'est pas satisfaite.

Si `soffice` est présent, rendre le DOCX pour vérifier visuellement et compter les pages :

```bash
soffice --headless --convert-to pdf --outdir /tmp/x export.docx   # puis pdf-export-validation
```

## Règles de construction (python-docx 1.2)

- Une **section par page source** en mode page par page : `doc.add_section(WD_SECTION.NEW_PAGE)` ; `section.page_width/height` et marges en `Emu`/`Pt`. Le changement de section crée le saut de page : ne pas ajouter en plus un `page_break` (page blanche).
- Styles : créer une fois `Titre 1`, `Titre 2`, `Corps`, `Légende`, `En-tête`, `Pied`, `Note`, `Liste` dans `doc.styles` ; chaque paragraphe référence un style et ne porte en direct que les écarts (surcharge utilisateur). Un changement global = modifier le style.
- Colonnes : `sectPr/w:cols w:num="2" w:space="..."` sur une section continue (`WD_SECTION.CONTINUOUS`) ; `w:br w:type="column"` pour forcer le passage à la colonne suivante.
- Image inline : `run.add_picture(path, width=Emu(...))` en gardant le ratio.
- Image flottante ou fond : python-docx ne sait pas créer `wp:anchor` ; convertir l'inline produit en ancre (XML `wp:anchor` avec `positionH/positionV relativeFrom="page"`, `behindDoc="1"` pour un fond, `wrapNone` ou `wrapSquare`). Garder `distT/distB/distL/distR`, `simplePos`, `relativeHeight`, `locked`, `layoutInCell`, `allowOverlap` : Word refuse l'ancre s'il en manque un.
- Texte positionné : paragraphe avec `w:pPr/w:framePr` (`w`, `h`, `x`, `y`, `hAnchor="page"`, `vAnchor="page"`, `hRule="exact"` ou `atLeast`, `wrap="none"`). Plus simple et mieux relu par LibreOffice que les zones de texte DrawingML ; la décision finale dépend du test de rendu (TECHNICAL_DECISIONS D-07).
- En-têtes/pieds détectés : soit dans `section.header` (si répétés), soit comme blocs positionnés de la page (si propres à la page). `is_linked_to_previous = False` quand le contenu diffère.

## Critères de validation (un fichier créé n'est pas un succès)

1. ZIP valide et ouverture par python-docx.
2. Texte de chaque bloc présent, chaque bloc dans **son propre** paragraphe (ou cadre).
3. Nombre d'images = nombre d'images attendues (illustrations + fonds).
4. Sections = pages source en mode page par page ; aucune page vide involontaire après rendu.
5. Aucun texte remplacé par une image ; tout repli listé dans le rapport d'export.
