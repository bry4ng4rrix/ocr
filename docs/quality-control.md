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

## Numérisation fidèle (PDF image + OCR invisible)

Services : `services/image_quality_service.py` (mesures), `services/image_enhancement.py` (plan), `services/pdf_export.py` (validation du PDF). Aucune note globale, aucune décision IA.

### Mesures par page (`ImageMetrics`)

Mesures faites sur l'intérieur de la page : les bandes de fond restées aux bords sont exclues.

| Mesure | Définition | Usage |
|---|---|---|
| `paper_level` | 90e centile de la luminance | papier gris (< 185) → gain d'exposition vers 228, ×1,41 au plus |
| `tonal_range` | 98e − 2e centile | < 90 (ou < 120 si Gemma le signale) → contraste modéré |
| `illumination_unevenness` | écart relatif du fond papier estimé | > 0,30 (ou > 0,18 si Gemma signale une ombre) → correction des ombres |
| `noise_sigma` | bruit de luminance (Immerkær, pleine résolution) | > 3 (ou > 1,8 si signalé) → débruitage léger |
| `sharpness` | variance du Laplacien | < 40 : flou fort → vérification, aucune « restauration » ; 40 à 150 et signalé → netteté modérée |
| `colorfulness` | indice de Hasler et Süsstrunk | > 15 : page colorée → aucune balance des blancs automatique |
| `dark_edges` | largeur de la bande de fond sur chaque bord | 0,1 à 8 % → rognage de ce bord (+0,4 %) ; ≥ 8 % → contour douteux, vérification |
| `border_content` | encre dans la bande de 1 % des bords | contenu peut-être coupé → vérification |
| `residual_skew_deg` | inclinaison mesurée (profil de projection) | contrôle |

### Contrôle avant/après (`QualityCheck`)

L'amélioration est **écartée** et la page redressée conservée si l'un de ces cas est mesuré :
- netteté < 55 % de l'avant ;
- +4 points de pixels écrêtés (noirs ou blancs) ;
- contraste < 60 % de l'avant ;
- dérive chromatique moyenne (a\*b\*) > 8 ;
- couleur divisée par plus de 2 sur une page colorée ;
- encre apparue au bord.

Un changement de clarté voulu, comme l'exposition, ne compte pas comme une dérive. **Une page n'est jamais déclarée réussie si le contrôle échoue** : elle passe en `REVIEW_REQUIRED`.

### États de file

| État | Signification |
|---|---|
| `PENDING` | à traiter (nouvelle page, page relancée, reprise après interruption) |
| `PROCESSING` | en cours ; remis en `PENDING` au redémarrage |
| `COMPLETED` | image finale et OCR à jour, aucun motif de vérification (ou page validée) |
| `REVIEW_REQUIRED` | au moins un motif : orientation incertaine, contour absent ou douteux, conflit d'orientation signalé par Gemma, amélioration écartée, flou fort, aucun texte reconnu… |
| `FAILED` | erreur (motif enregistré), ou page interrompue 3 fois (`BOOKOCR_SCAN_MAX_ATTEMPTS`) ; relance sélective possible |

Une confiance OCR basse **ne bloque pas** : le client n'exige pas de correction du texte, l'OCR sert à la recherche.

### Validation du PDF du livre (`exports/<livre>_numerisation_<date>.validation.json`)

- nombre de pages = nombre de pages du livre, dans l'ordre ;
- texte extractible sur chaque page qui a des mots OCR ;
- couche texte **100 % invisible** (mode de rendu 3 pour chaque élément, aucun texte blanc) ;
- flux image du PDF **identique octet pour octet** au JPEG final (pas de recompression ni de rotation) ;
- écart moyen Lab entre le rendu PDF et l'image finale ≤ 6 ;
- recherche de mots témoins (les plus longs mots OCR fiables de chaque page).
