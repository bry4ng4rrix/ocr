---
name: update-docs
description: Mettre à jour la documentation technique (ARCHITECTURE, TECHNICAL_DECISIONS, KNOWN_LIMITATIONS, TEST_STRATEGY, README, CHANGELOG, planning) pour qu'elle corresponde au code réel.
disable-model-invocation: true
argument-hint: "[sujet ou référence git]"
---

# Mise à jour de la documentation

Sujet : `$ARGUMENTS`

- Changements depuis le dernier commit : !`git status --short | head -40`
- Modules présents : !`ls services models components 2>/dev/null | tr '\n' ' '`

## Étapes

1. Lister ce qui a changé (`git diff HEAD`, ou `git log` sur la référence donnée).
2. Pour chaque document, vérifier contre le code — pas contre la mémoire :
   - `docs/ARCHITECTURE.md` : signatures publiques (`grep -n "^def \|^class " services/*.py models/*.py`) et diagramme Mermaid (imports réels).
   - `docs/TECHNICAL_DECISIONS.md` : nouvelle décision ou décision remplacée.
   - `docs/KNOWN_LIMITATIONS.md` : limites constatées par les tests (`xfail`, `skip`).
   - `docs/TEST_STRATEGY.md` : matrice exigences → tests (les fichiers de test cités existent).
   - `README.md` : chaque commande citée existe et fonctionne (la lancer si elle est rapide).
   - `CHANGELOG.md` : une entrée datée par changement significatif.
   - `planning.md` : états cohérents ; `DONE` seulement si les critères sont vérifiés.
3. Appliquer les modifications, puis montrer le diff de la documentation et signaler ce qui n'a pas pu être vérifié.
