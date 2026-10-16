---
name: project-plan
description: Construire ou mettre à jour le plan de développement (roadmap.md et planning.md) - tâches, dépendances, priorités, propriétaires, critères d'acceptation et états.
disable-model-invocation: true
argument-hint: "[objectif ou changement de périmètre]"
---

# Mise à jour du plan

Objectif ou changement demandé : $ARGUMENTS

## Données relevées

- Tâches actuelles : !`grep -cE '^\| T-' planning.md 2>/dev/null || echo 0` tâches dans planning.md
- Changements depuis le dernier commit : !`git status --short | head -30`

## Étapes

1. Lire `roadmap.md`, `planning.md`, `docs/ARCHITECTURE.md` et le code concerné.
2. Si l'objectif est nouveau : le rattacher à une phase de `roadmap.md` (ou en créer une avec objectifs et critères d'acceptation).
3. Découper en tâches de `planning.md` au format existant : `| T-xx | tâche | phase | priorité P0–P2 | dépendances | propriétaire (sous-agent) | critères d'acceptation vérifiables | état |`.
4. Une tâche qui touche plusieurs modules commence par une tâche « contrat » pour `software-architect`.
5. États : ne jamais passer une tâche à `DONE` dans cette commande ; mettre `BLOCKED` avec la cause précise si une dépendance externe manque.
6. Repérer les tâches parallélisables (fichiers disjoints) et les indiquer dans la section « Prochain lot » de `planning.md`.
7. Montrer le diff de `roadmap.md` et `planning.md` et résumer les changements.
