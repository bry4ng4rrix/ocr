# Règles du projet — Reconstruction de livres imprimés (DOCX / PDF)

Ce fichier est la **source unique** des règles de développement. `CLAUDE.md` l'importe ; ne pas recopier ces règles ailleurs.

## Projet en une phrase

Application locale Python + Streamlit qui reconstruit un livre modifiable (DOCX) et un livre numérique (PDF) à partir de photos de pages : correction géométrique automatique, OCR, détection de mise en page, extraction des images, édition visuelle, pagination maîtrisée.

Documents de référence : [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) (modules et contrats), [docs/TECHNICAL_DECISIONS.md](docs/TECHNICAL_DECISIONS.md), [docs/TEST_STRATEGY.md](docs/TEST_STRATEGY.md), [docs/KNOWN_LIMITATIONS.md](docs/KNOWN_LIMITATIONS.md), [roadmap.md](roadmap.md), [planning.md](planning.md), [CHANGELOG.md](CHANGELOG.md).

## Environnement

- Python : `.venv/bin/python` (3.14 testé, 3.12+ visé). Tests : `.venv/bin/python -m pytest -q`.
- Dépendances système : Tesseract 5 (+ langues `tessdata`), LibreOffice headless (`soffice`) **facultatif** pour la conversion DOCX → PDF.
- Données de travail : `data/` (jamais versionné). Code : hors de `data/`.

## Règles de code

1. **Architecture modulaire** : l'interface (`app.py`, `components/`) n'appelle que `services/` ; aucune logique métier dans un composant Streamlit. Les services ne dépendent jamais de Streamlit.
2. **Typage** : annotations sur toutes les fonctions publiques ; modèles de données en Pydantic v2 (`models/`). Images en mémoire : `numpy.ndarray` RGB `uint8 (H, W, 3)` ou gris `uint8 (H, W)`.
3. **Fonctions courtes et testables** : calcul pur séparé des entrées/sorties ; une fonction = une responsabilité.
4. **Exceptions explicites** : erreurs métier dans `models/errors.py` avec un message utilisateur en français ; jamais de `except Exception: pass`. Toute erreur attrapée est journalisée ou remontée.
5. **Noms explicites** pour classes, fonctions et variables (pas d'abréviations obscures).
6. **Coordonnées** : un seul repère, normalisé 0..1 sur l'image corrigée de la page, origine en haut à gauche (voir ARCHITECTURE §4). Conversion en points uniquement dans `services/composition_service.py`.

## Règles sur les données

7. **Les originaux ne sont jamais modifiés ni écrasés** (`data/projects/<id>/originals/` en lecture seule pour le code).
8. **Résultats intermédiaires sauvegardés** (image corrigée, OCR, régions, illustrations) et régénérables depuis l'original + paramètres.
9. **Aucune transformation destructive sans retour possible** : les réglages sont des paramètres réappliqués, l'historique permet de restaurer une version.
10. **Pas de secrets** dans le code ni dans les fichiers versionnés. Aucun envoi de document à un service externe.

## Règles de travail

11. **Tests** pour chaque fonctionnalité importante (voir TEST_STRATEGY). Un test qui échoue n'est ni supprimé ni désactivé pour « passer » ; une limite de bibliothèque se marque `xfail` avec la raison et une entrée dans KNOWN_LIMITATIONS.
12. **Rien n'est « terminé » sans vérification réelle** : tests exécutés, fichiers générés ouverts et relus (texte, images, nombre de pages). Une tâche passe à `DONE` dans `planning.md` seulement quand ses critères d'acceptation sont vérifiés.
13. **Pas de suppression de fonctionnalité** existante sans autorisation de l'utilisateur.
14. **Pas de réécriture générale** sans justification technique écrite dans TECHNICAL_DECISIONS.
15. **Documentation à jour** après tout changement important : ARCHITECTURE (contrats), KNOWN_LIMITATIONS, CHANGELOG, `planning.md`.
16. **Compte rendu fidèle** : fichiers modifiés, commandes de test lancées et leur résultat, problèmes restants. Ne jamais affirmer qu'une opération a été exécutée si elle ne l'a pas été.

## États des tâches

`TODO`, `IN_PROGRESS`, `BLOCKED` (avec la cause), `DONE` (critères vérifiés).
