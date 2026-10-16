---
name: project-code-review
description: Revue des changements de ce projet selon ses contrats et ses règles - conception, frontières interface/services, gestion des erreurs, préservation des originaux, non-destructivité, cohérence aperçu/export, tests manquants, régressions. À utiliser avant de déclarer une tâche terminée ou quand on demande d'examiner des modifications.
---

# Revue de code du projet

Complète la commande intégrée `/code-review` (recherche générale de bogues) avec les points propres à ce projet.

## Périmètre

Par défaut : `git diff HEAD` + fichiers non suivis (`git status --short`). Lire chaque fichier modifié **en entier** autour des changements, et les appelants (`grep -rn "nom_de_fonction"`).

## Liste de contrôle

**Architecture**
- `components/` ou `app.py` n'importe que `services/` et `models/` ; aucun calcul d'image, OCR, DOCX ou PDF dans l'interface.
- Aucun `import streamlit` dans `services/` ou `models/`.
- Signatures publiques conformes à docs/ARCHITECTURE.md ; tout écart est soit corrigé, soit documenté.

**Données et non-destructivité**
- Aucune écriture dans `originals/` ; aucun `cv2.imwrite`/`save` sur un chemin d'entrée.
- Toute image dérivée est régénérable depuis l'original + paramètres enregistrés.
- `project.json` : écriture atomique (fichier temporaire puis `os.replace`), `schema_version` respecté, migration si un champ change.

**Erreurs**
- Pas de `except Exception` muet ; les erreurs utilisateur passent par `models/errors.py` avec un message en français.
- Sous-processus (tesseract, soffice) : délai, code de retour vérifié, stderr journalisé.

**Fidélité et exports**
- Aperçu et exports utilisent la même composition (`composition_service`).
- Aucun texte remplacé par une image sans entrée dans le rapport d'export ; aucun saut de page supprimé.
- Les changements globaux de police respectent les surcharges individuelles.

**Tests**
- Chaque comportement nouveau ou corrigé a un test ; les tests relisent les fichiers produits.
- Aucun test supprimé, affaibli ou mis en `skip` sans raison valable.

## Format du compte rendu

Liste classée par gravité : `[bloquant|important|mineur] fichier:ligne — problème — scénario concret qui le déclenche — correction proposée`. Terminer par les tests lancés pendant la revue et leur résultat. Pas de constat sans scénario vérifiable.
