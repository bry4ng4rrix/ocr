---
name: validate-exports
description: Tester réellement les exports DOCX et PDF - tests d'export, puis contrôle des fichiers produits (texte, images, pages, blancs, polices, conversion LibreOffice).
disable-model-invocation: true
argument-hint: "[fichier .docx/.pdf à contrôler]"
---

# Valider les exports

Fichier fourni : `$ARGUMENTS` (vide = valider à partir des tests).

- LibreOffice : !`command -v soffice || echo "absent : conversion DOCX→PDF non vérifiable"`
- PyMuPDF : !`.venv/bin/python -c "import pymupdf; print(pymupdf.__version__)" 2>&1 | tail -1`
- Tests d'export présents : !`ls tests/test_document_service.py tests/test_pdf_service.py 2>&1`

## Étapes

1. **Fichier fourni** : `.docx` → `.venv/bin/python .claude/skills/docx-export-validation/scripts/check_docx.py FICHIER` ; `.pdf` → `.venv/bin/python .claude/skills/pdf-export-validation/scripts/check_pdf.py FICHIER --render <scratchpad>`. Puis regarder au moins deux pages rendues (Read sur les PNG).
2. **Sans fichier** : lancer `.venv/bin/python -m pytest -q -rs tests/test_document_service.py tests/test_pdf_service.py tests/test_integration.py`. Si ces tests n'existent pas encore, le dire et s'arrêter : rien n'est validé.
3. Si `soffice` est présent, convertir le DOCX de test en PDF et le contrôler avec `check_pdf.py --expect-pages N`.
4. Compte rendu : pour chaque fichier, verdict par critère (ouverture, texte, images, nombre de pages, pages blanches, polices, débordements) ; ce qui n'a pas pu être vérifié et pourquoi. Ne jamais déclarer un export valide parce que le fichier existe.
