# Numérisation intelligente avec Gemma 3 (Ollama local)

État : **19/10/2026**. Contrat : [ARCHITECTURE.md §8](ARCHITECTURE.md). Contrôles : [quality-control.md](quality-control.md).

## 1. Ce que fait le module

Un **PDF par livre**. Chaque page du PDF montre la **photographie traitée** : redressée, recadrée, orientée, exposition et contraste corrigés avec prudence, couleurs conservées. Une **couche OCR invisible** y est posée, alignée mot par mot, sélectionnable et recherchable. Le PDF n'est **jamais** une reconstruction du livre ni une image générée. La reconstruction DOCX reste un module séparé (écrans Éditeur et Export).

Écran : **Traitement › Numérisation intelligente** (`components/screens/smart_scan.py`).

## 2. Rôle de Gemma : assistant d'analyse, jamais exécutant

| Étape | Qui décide | Gemma |
|---|---|---|
| Bords, perspective, recadrage | OpenCV (`orientation_service.detect_page_quad`, quadrilatère validé) | jamais de coordonnées |
| Orientation | EXIF + OSD/lecture Tesseract | s'il voit encore la page tournée : **vérification demandée**, aucune rotation |
| Inclinaison, courbure | mesures OpenCV | courbure forte signalée → vérification |
| Ombres, exposition, contraste, bruit, netteté | `image_enhancement.plan_adjustments` sur des **mesures** | une action proposée n'est retenue que si une mesure la confirme |
| Couleurs | mesure de la couleur (Hasler) | `PRESERVE_COLOR` / `full_color` : aucune balance des blancs automatique |
| Texte OCR | Tesseract | **jamais** corrigé ni réécrit par Gemma |

La réponse de Gemma est contrainte par un schéma JSON. Ollama 0.40.6 accepte le schéma Pydantic, `$defs` compris, dans `format`. Elle est ensuite validée par `models.scanning.VisualAnalysis` :
- catégories bornées : `none` / `low` / `medium` / `high` / `uncertain`, etc. ;
- actions dans une **liste blanche** : `ROTATE`, `DETECT_PAGE`, `PERSPECTIVE_CORRECTION`, `SHADOW_CORRECTION`, `LIGHTING_CORRECTION`, `CONTRAST_ADJUSTMENT`, `DENOISE`, `SHARPEN`, `PRESERVE_COLOR`, `MANUAL_REVIEW` ;
- tout champ inconnu est refusé (`extra="forbid"`) ;
- `reason` est nettoyé de ses caractères de contrôle et borné à 400 caractères ; il sert seulement à l'affichage.

Le pipeline n'exécute aucun texte du modèle : ni code, ni commande, ni chemin. Une réponse invalide donne le statut `invalid_response` et le traitement **mesuré** continue.

## 3. Pipeline d'une page

1. **Import** (existant) : original copié sous `originals/<sha256[:16]>`, jamais réécrit ; EXIF appliqué à la lecture ; formats et dimensions contrôlés.
2. **Géométrie** (existant, `processing_service.run_geometry`, avec le secours `page_bounds` par la couleur du papier) : feuille, perspective, quart de tour (OSD et lecture), inclinaison, courbure prudente → `corrected/<page>.png`. Les incertitudes deviennent des motifs de vérification.
3. **Analyse Gemma** (facultative) : copie JPEG réduite à **896 px**. Gemma 3 traite toute image en 896 × 896, soit 256 jetons ; envoyer plus grand ne sert à rien (mesure : 297 jetons d'entrée au total). Une page par requête, une requête à la fois. Le résultat est mis en cache selon l'empreinte SHA-256 de l'image envoyée, le modèle, son digest et la version de la consigne.
4. **Mesures** (`image_quality_service.measure`) sur la page redressée.
5. **Plan** (`image_enhancement.plan_adjustments`) : mesures seules, ou mesures + Gemma.
6. **Rendu** (`apply_adjustments`) puis **contrôle avant/après** (`compare`). En cas de dégradation, la page redressée est gardée et la page passe en vérification.
7. **Image finale** : `scan/<page>/final.jpg`, seul encodage avec perte (qualité 92, 4:4:4, sans EXIF).
8. **OCR** Tesseract sur l'image finale → `scan/<page>/ocr.json`, boîtes normalisées sur `final.jpg`.
9. **État** dans la file SQLite : `COMPLETED`, `REVIEW_REQUIRED` ou `FAILED`.

## 4. Modes

| Réglage | Effet |
|---|---|
| `OLLAMA_ENABLED=false` (défaut du code) | Aucun appel. Tout fonctionne avec OpenCV + Tesseract. |
| `OLLAMA_AUTO_APPLY=false` (défaut) | Le traitement applique le **plan mesuré**. Le plan Gemma (confirmé par les mesures) est rangé en **proposition** : aperçu dans l'écran, puis « Appliquer la proposition ». |
| `OLLAMA_AUTO_APPLY=true` | Le plan combiné est appliqué ; ses motifs de vérification (actions géométriques, demande de vérification, courbure forte) passent la page en `REVIEW_REQUIRED`. |
| Réglages manuels | Jamais écrasés par un nouveau traitement ; historique et retour arrière. |

Les curseurs de l'écran recalculent un aperçu réduit avec OpenCV, **sans** appeler Gemma.

## 5. Configuration

Variables d'environnement, ou fichier `.env` à la racine (lu par `config.py`, voir [.env.example](../.env.example)) :

```env
OLLAMA_ENABLED=true
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_VISION_MODEL=gemma3:4b
OLLAMA_TIMEOUT=120
OLLAMA_MAX_RETRIES=2
OLLAMA_ANALYSIS_ENABLED=true
OLLAMA_AUTO_APPLY=false
OLLAMA_MAX_PARALLEL_REQUESTS=1
```

Aucune clé n'est nécessaire pour le serveur local. **Aucun modèle n'est téléchargé** : un modèle absent est signalé (`/api/show` 404), et l'application continue sans IA.

API utilisée : `GET /api/tags` (modèles, digest), `POST /api/show` (capacités `vision`), `POST /api/chat` (`images` base64, `format` = schéma JSON, `stream: false`, `temperature: 0`). Les nouvelles tentatives ne portent que sur un délai dépassé, une erreur réseau ou un HTTP 5xx ; elles sont bornées. Le journal ne contient ni image ni texte de page.

## 6. Mesures réelles sur ce poste (19/10/2026)

Matériel : 4 cœurs, 11 Go de RAM, GTX 1050 Ti 4 Go. **Ollama n'utilise pas le GPU** (`/api/ps` : `size_vram: 0`).

| Mesure | Valeur |
|---|---|
| Analyse Gemma d'une page (896 px, CPU) | 63 à 85 s avec une consigne courte ; **113 à 117 s** avec la consigne de production, mesurées sur 2 pages réelles |
| Chargement du modèle (premier appel) | 13,6 s |
| Réponse servie par le cache | 0,5 à 0,7 s |
| Traitement sans IA (géométrie + rendu + OCR) | **16,7 s/page** en moyenne sur 4 photos réelles de 20 Mpx (géométrie 5,6 à 14,9 s, OCR 4,9 à 8,6 s) |

Projection à vérifier sur un échantillon plus grand avant d'en faire une promesse : 8 400 pages ≈ 39 h sans IA, et environ **270 h de plus** avec Gemma sur ce poste. Avec ce matériel, l'analyse Gemma se réserve aux pages en vérification, en lançant l'analyse page par page.

**Qualité observée de gemma3:4b** sur les pages réelles :
- pages de livre en noir et blanc classées « photographic » ;
- `ROTATE` recommandé alors que lui-même répondait `orientation_issue: none` ;
- page à l'envers déclarée « none » ;
- `manual_review_required: true` pour une page qu'il jugeait lui-même « sans défaut visible ».

Ces incohérences justifient la règle « Gemma propose, les mesures décident ». Les actions incohérentes (`ROTATE` sans défaut d'orientation) sont ignorées.

## 7. Fonctionnement sans IA

C'est le mode par défaut et le mode de repli. Si Ollama est désactivé, injoignable, sans le modèle, trop lent ou s'il répond hors schéma, chaque page est traitée par les mesures et le statut de l'analyse est enregistré (`disabled`, `unavailable`, `timeout`, `invalid_response`). L'écran n'affiche « Disponible » qu'après un test réel : serveur joignable, modèle installé, capacité vision.

## 8. Tests

- `tests/test_ollama_analysis.py` (faux serveur HTTP local, sans modèle) : disponibilité, modèle absent, réponse valide, réponses invalides (hors liste blanche, catégorie inconnue, champ inconnu, champ manquant, JSON illisible), délai dépassé avec tentatives bornées, serveur indisponible, HTTP 5xx puis succès, HTTP 4xx sans nouvelle tentative, cache.
- `tests/test_ollama_integration.py` : **vrai** modèle, désactivé par défaut. Lancement :
  `BOOKOCR_TEST_OLLAMA=1 .venv/bin/python -m pytest -q -m ollama tests/test_ollama_integration.py`
