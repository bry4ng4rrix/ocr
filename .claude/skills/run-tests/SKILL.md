---
name: run-tests
description: Lancer les tests adaptés aux fichiers modifiés, puis la suite complète, et rendre compte fidèlement des résultats.
disable-model-invocation: true
argument-hint: "[all | chemin ou motif -k]"
---

# Lancer les tests

Argument : `$ARGUMENTS` (vide = tests ciblés puis suite complète ; `all` = suite complète seulement).

Fichiers modifiés : !`git status --short | awk '{print $2}' | head -40`

## Étapes

1. Sélectionner les tests ciblés avec le tableau « Choisir les tests à lancer » du skill `python-testing` (le charger si besoin).
2. Lancer les tests ciblés : `.venv/bin/python -m pytest -q -rs <fichiers>`.
3. S'ils passent, lancer la suite complète : `.venv/bin/python -m pytest -q -rs`.
4. Pour chaque échec : relancer seul avec `-x -vv --tb=long`, puis classer (défaut du code, test erroné, limite de bibliothèque, environnement). **Ne modifier ni le code ni les tests dans cette commande.**
5. Compte rendu : commandes exactes, nombres passés / échoués / ignorés, raison de chaque test ignoré, diagnostic de chaque échec avec le fichier probablement en cause et son propriétaire.
