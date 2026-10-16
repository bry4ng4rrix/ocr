---
name: layout-reconstruction
description: Transformer des mots et lignes OCR et des régions d'image en blocs documentaires structurés (titres, paragraphes, colonnes, listes, légendes, en-têtes, pieds, numéros, notes, tableaux), classer la page (cas A texte, B fond graphique, C photo + texte), calculer l'ordre de lecture, le format physique et la composition en points. À utiliser pour layout_service, composition_service ou typography_service.
---

# Reconstruction de la mise en page

Contrats : docs/ARCHITECTURE.md §4–§6. Ce skill donne la méthode.

## 1. Des mots aux blocs

1. Lignes : regrouper les mots par `(block, par, line)` de Tesseract, puis **re-découper** une ligne si un écart horizontal > 3 × hauteur de ligne (gouttière de colonnes que Tesseract a fusionnée).
2. Paragraphes : enchaîner deux lignes si recouvrement horizontal ≥ 50 %, interligne ≤ 1,6 × l'interligne médian du bloc et hauteur de x comparable (±25 %). Un retrait de première ligne ou une ligne précédente courte (< 70 % de la largeur) termine un paragraphe.
3. Césure : `mot-` en fin de ligne + minuscule au début de la suivante → recoller sans tiret ; garder le tiret sinon.
4. Ne jamais produire un bloc unique pour toute la page : si c'est le résultat, c'est un défaut à signaler.

## 2. Types de blocs (règles, puis correction par l'utilisateur)

| Type | Indices |
|---|---|
| `heading_1` / `heading_2` | taille ≥ 1,6× / 1,25× le corps, ≤ 3 lignes, souvent centré ou gras |
| `header` / `footer` | bande haute / basse (8 % de la page), petite taille, répété d'une page à l'autre |
| `page_number` | chiffres arabes ou romains courts (≤ 6 caractères, `^[ivxlcdm]{1,6}$` ou `^\d{1,4}$`) dans les bandes haute ou basse |
| `caption` | juste sous ou sur une image (≤ 4 % de hauteur d'écart), largeur ≤ image, taille ≤ corps |
| `list_item` | puce (`•–-*`) ou `^\d+[.)]` en début de ligne |
| `note` | bas de page sous un filet ou taille < 0,85× le corps avec appel numérique |
| `table` | réglures détectées ou colonnes alignées sur ≥ 3 lignes ; signalé pour vérification |
| `paragraph` | défaut |

## 3. Colonnes et ordre de lecture

Bandes horizontales pleine largeur (titres, images larges) séparées par des zones multicolonnes ; dans chaque zone : colonnes de gauche à droite, blocs de haut en bas. En-têtes en premier, pieds et numéros en dernier. Tester avec une page synthétique à deux colonnes + titre pleine largeur.

## 4. Catégories de page

- **A – texte** : surface d'images < 5 %, fond clair uniforme.
- **B – fond graphique** : > 40 % de la page non-papier (Lab : L < 80 ou chroma > 18 sur grandes zones) **et** du texte dans ces zones.
- **C – photo + texte** : une ou plusieurs illustrations nettes à bords francs, texte hors des illustrations.
- **Texte sur image** : blocs dont ≥ 50 % de la boîte est dans une illustration → gardés (jamais jetés), marqués `on_image=True`.
- **Mixte** : plusieurs des cas ci-dessus.

## 5. Format physique et tailles

- Taille de police en points = hauteur de x en pixels / (0,47 × pixels par point) ; `pixels par point = largeur de l'image / largeur de page en points`.
- Format détecté : comparer le ratio largeur/hauteur aux formats courants (A4, A5, B5, 13×20, 15,5×24, 11×17,8, Letter) **et** vérifier que le corps estimé tombe entre 8 et 13 pt ; sinon proposer A5 avec un avertissement.

## 6. Vérifier le rendu

La mise en page n'est pas « correcte » tant que la page composée n'a pas été rasterisée et comparée à la source : superposer les boîtes, vérifier qu'aucun bloc ne déborde (rapport de `pdf_service`) et que l'ordre de lecture du texte extrait du PDF suit celui des blocs.
