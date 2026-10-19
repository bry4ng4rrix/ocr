# Contrôle de qualité

Service : `services/quality_service.py`. Routage : `services/routing_service.py`.

Les bandes ne reposent pas sur une note globale unique.

| Bande | Signification | Critères typiques |
|---|---|---|
| A | Fiable | Page analysée, confiance ≥ `BOOKOCR_ROUTE_RELIABLE` (80), pas de bloc vide, illustrations présentes |
| B | Douteuse | Page pas encore OCR, confiance intermédiaire, géométrie changée après OCR |
| C | Complexe | Confiance < `BOOKOCR_ROUTE_DOUBTFUL` (60) |
| D | Candidat cloud | C + Gemini activé et `cloud_ai_allowed` ; **aucun appel automatique** |
| E | Relecture | Erreur, texte vide, illustration manquante, ou C sans aucun secours IA autorisé |

Le livre est aussi comparé au `expected_page_count` s’il est renseigné.

Ollama et Gemini restent **éteints par défaut**. Le pipeline local n’envoie aucune page.
