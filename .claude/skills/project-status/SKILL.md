---
name: project-status
description: Analyser l'avancement réel du projet et les tâches restantes à partir de planning.md, roadmap.md, de l'état Git et des tests.
disable-model-invocation: true
---

# État du projet

## Données relevées au lancement

- Branche et changements : !`git status --short --branch | head -40`
- Derniers commits : !`git log --oneline -8`
- Tâches par état : !`grep -oE '\b(TODO|IN_PROGRESS|BLOCKED|DONE)\b' planning.md | sort | uniq -c`
- Tâches bloquées : !`grep -E 'BLOCKED' planning.md || echo "aucune"`
- Outils : !`tesseract --version 2>&1 | head -1; tesseract --list-langs 2>&1 | tail -n +2 | tr '\n' ' '; echo; command -v soffice || echo "soffice absent"`

## À faire

1. Lancer `.venv/bin/python -m pytest -q -rs 2>&1 | tail -25` et relever passés / échoués / ignorés (avec les raisons).
2. Lire `planning.md` et `roadmap.md`. Pour chaque tâche `DONE` de la phase en cours, vérifier qu'un test correspondant existe et passe ; signaler toute tâche `DONE` sans preuve.
3. Rendre compte, sans rien modifier :
   - phase en cours de la roadmap et pourcentage de tâches `DONE` vérifiées ;
   - tâches `IN_PROGRESS` et `BLOCKED` (avec cause et action attendue de l'utilisateur) ;
   - résultat des tests ;
   - les 3 à 5 prochaines tâches réalisables (dépendances satisfaites), avec le sous-agent propriétaire.
