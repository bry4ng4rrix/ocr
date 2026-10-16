---
name: review-changes
description: Examiner les changements récents (diff de travail ou commits indiqués) selon la liste de contrôle du projet, sans les modifier.
disable-model-invocation: true
argument-hint: "[référence git, ex. HEAD~3]"
---

# Revue des changements

Référence : `$ARGUMENTS` (vide = changements non commités).

- Résumé du diff : !`git diff --stat HEAD 2>/dev/null | tail -20`
- Fichiers non suivis : !`git status --short | grep '^??' | head -20`

## Étapes

1. Charger le skill `project-code-review` et appliquer sa liste de contrôle.
2. Diff à examiner : `git diff HEAD` si aucun argument, sinon `git diff $ARGUMENTS..HEAD` et `git log --oneline $ARGUMENTS..HEAD`.
3. Lancer les tests des modules touchés (voir `python-testing`) pour confirmer ou infirmer chaque constat.
4. Rendre le compte rendu au format de `project-code-review`. Aucune modification de fichier dans cette commande ; proposer les corrections et leur propriétaire.
