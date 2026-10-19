# Limitations connues

Propriétaire : `integration-documentation`. Une limite est **constatée** (preuve : test ou mesure, citée) ou **attendue** (à confirmer par un test). Mise à jour à chaque phase.

## Environnement (constaté le 16/10/2026)

| Limite | Effet | Contournement |
|---|---|---|
| PaddlePaddle n'a aucune roue pour Python 3.14 | PaddleOCR est inutilisable dans cet environnement | Tesseract (D-02). PaddleOCR possible plus tard dans un venv Python 3.12 |
| Langue `deu` absente (installées : `afr`, `eng`, `fra`, `osd`) | OCR allemand impossible, alors que **les photos réelles disponibles sont en allemand** | `sudo pacman -S tesseract-data-deu` |
| LibreOffice absent | Pas de conversion DOCX → PDF ni de contrôle du rendu DOCX | `sudo pacman -S libreoffice-fresh`. Le PDF direct (PyMuPDF) reste disponible |
| ~~Paquet obsolète `docx` 0.2.4 dans `.venv`~~ | Corrigé le 16/10/2026 : désinstallé, python-docx 1.2.0 réinstallé, `pip check` sans erreur | — |
| OSD Tesseract sur la photo brute du banc | Échec (« Too few characters ») : la feuille est trop petite dans le cadre | Isoler la feuille **avant** l'OSD (ordre imposé dans `orientation_service`) |

## OCR (attendu)

- Tesseract est moins robuste que les OCR neuronaux sur les photos floues, très inclinées ou mal éclairées. La confiance est affichée et les mots faibles sont signalés.
- L'OSD (orientation) échoue sur les pages presque sans texte. La page reste alors à 0° avec un avertissement, et la rotation manuelle reste possible.
- Le texte sur fond photographique, les lettrines, les petites capitales et les polices décoratives sont souvent mal lus.
- Les tableaux sont reconnus comme du texte aligné. La structure en cellules n'est reconstruite que pour les tableaux réglés (attendu, à confirmer).

## Correction géométrique (attendu)

- La courbure n'est corrigée que si elle est mesurable de façon fiable (D-11). Une page très bombée près de la reliure reste déformée.
- La perspective n'est corrigée que si les quatre bords de la feuille sont visibles.
- Les pixels de fond cachés par le texte imprimé ne sont **pas** retrouvés. Le fond « nettoyé » est une estimation (inpainting), signalée comme telle, et l'image source est conservée.

## Mise en page (attendu)

- La police, la taille, le gras et l'italique sont **estimés**. La typographie exacte du livre n'est pas identifiée, mais tout reste modifiable.
- Le format physique de la page est estimé à partir du ratio et de la taille du corps de texte. Il faut le vérifier.

## DOCX : Word et LibreOffice (spike T-30 fait sans LibreOffice ; rendu à confirmer)

- **Constaté** : une zone de texte DrawingML (`wps`) est invisible pour python-docx. C'est pourquoi les cadres `w:framePr` ont été retenus (D-07).
- **Attendu** : les noms de styles « Titre 1 » et « Légende » sont aussi les noms affichés des styles intégrés de Word en français. Des doublons peuvent apparaître dans la galerie de styles.
- **Attendu** : Word fusionne deux paragraphes consécutifs dont les cadres sont identiques. LibreOffice peut laisser un paragraphe d'ancrage vide par cadre, avec un risque de page supplémentaire en mode fidélité. Le test de conversion compare le nombre de pages (T-53).
- **Attendu** : l'interligne « multiple » dépend des métriques de police, que Word et LibreOffice ne lisent pas de la même façon. En mode modifiable, le texte peut refluer et ajouter une page, ce qui ne se détecte qu'au rendu.
- Le numéro de page est gardé comme texte imprimé, pas comme champ PAGE. Les puces sont des caractères littéraux, pas une numérotation Word.

- Les cadres (`w:framePr`) et les images ancrées sont rendus avec de légers décalages selon le logiciel. Le mode `fidelity` est le plus sensible.
- Un cadre de hauteur exacte coupe le texte ajouté par l'utilisateur. La hauteur « au moins » fait déborder sur les blocs voisins. Le rapport d'export signale les blocs concernés.
- Les polices absentes de la machine du lecteur sont remplacées par Word ou LibreOffice. Le PDF, lui, intègre les polices.

## PDF

- PyMuPDF est sous licence AGPL-3.0. C'est sans conséquence en usage local, mais une contrainte en cas de redistribution (D-04).

## Numérisation PDF (fac-similé)

- Les contours OpenCV sont des propositions : reliure sombre, fond de couleur proche du papier, bords masqués ou pages fortement courbées demandent souvent un cadrage manuel.
- La séparation d'une double page utilise une ligne de reliure réglable ; elle ne redresse pas encore indépendamment les deux plans perspectifs d'une double page courbée. Vérifier l'aperçu avant validation.
- L'ajustement manuel actuel est rectangulaire dans l'image source ; les quatre coins d'une page en forte perspective ne sont pas éditables séparément dans cette première version.
- L'ordre se modifie dans le tableau avant export. La suppression exige une case de confirmation.
- Les images actives sont gardées dans l'état de session Streamlit jusqu'à sa réinitialisation ; pour de très gros lots, travailler par lots évite de conserver toutes les sources haute résolution en RAM.
- Ollama est facultatif et requiert `OLLAMA_ENABLED=true`, `OLLAMA_BASE_URL` (par défaut `http://localhost:11434`) et `OLLAMA_VISION_MODEL` (par défaut `gemma3:4b`). Le modèle doit être installé séparément ; aucun téléchargement automatique n'est lancé.

## Numérisation intelligente (constaté le 19/10/2026)

| Limite | Effet | Contournement |
|---|---|---|
| Ollama tourne sur le CPU de ce poste (GPU 1050 Ti non utilisé, `size_vram: 0`) | Analyse Gemma de 113 à 117 s par page avec la consigne de production (mesuré sur 2 pages) : environ 270 h pour 8 400 pages | traitement sans IA par défaut (16,7 s/page mesurées) ; Gemma page par page sur les pages à vérifier ; serveur Ollama avec GPU |
| gemma3:4b peu fiable sur ces pages (page à l'envers vue « none », `ROTATE` incohérent, `manual_review_required` contradictoire) | Une décision fondée sur Gemma seul serait fausse | Gemma propose, les mesures décident (D-16) ; un conflit d'orientation demande une confirmation, jamais une rotation |
| Rapport largeur/hauteur après perspective = rapport **apparent** (arêtes maximales du quadrilatère, `orientation_service.warp_quad`) | Sous forte perspective, la page peut être un peu étirée (synthétique très déformé : 0,63 obtenu pour 0,70 réel) | photographier perpendiculairement (banc) ; estimation de focale à étudier |
| Contour de feuille légèrement trop large sur le banc (bande noire d'environ 0,3 % sur un ou deux bords) | Liseré noir dans le PDF | rogné automatiquement par bord après mesure (`dark_edges`) ; bande ≥ 8 % : vérification |
| Orientation des pages peu textuelles (graphique tourné, couverture à titre vertical) souvent incertaine | Page en `REVIEW_REQUIRED`, non tournée | rotation manuelle dans l'écran |
| Courbure : homographie + correction prudente existante (D-11) | Une page très bombée reste déformée près de la reliure | signalée à la vérification ; nouvelle photo |
| Images intermédiaires : `corrected/<page>.png` (environ 6 à 10 Mo pour une page de 20 Mpx recadrée) + `final.jpg` (1 à 2 Mo) | Environ 60 à 100 Go pour 8 400 pages (estimation à confirmer) | prévoir le disque ; tout est régénérable depuis les originaux et les paramètres |
| Les pages supprimées gardent leur dossier `scan/<page>/` | Espace disque | sans effet fonctionnel ; nettoyage manuel |
| L'écran `pdf_scan` (fac-similé de l'utilisateur) et `ollama_vision` restent séparés du nouveau module | Deux écrans de numérisation | à fusionner avec l'accord de l'utilisateur |
| Secours « couleur du papier » refusé quand la page touche le cadre sur 2 côtés ou plus | Le contour reste celui de l'ancien détecteur, ou la page passe en vérification | recadrage manuel ; photographier la page entière avec un peu de fond autour |
| Une photographie qui déborde jusqu'au bord de la page peut perdre jusqu'à 2,5 % de ce bord (critère « rangée sans papier »), ou jusqu'à 4 % si un filet continu longe ce bord | Rognage légèrement excessif | réglages « Rognage » dans « Ajuster manuellement » (0 = aucun) |
| Format uniforme du PDF : les pages de résolution différente sont mises à l'échelle (placement seulement, image non recompressée) | La taille imprimée ne correspond plus à la résolution d'origine | désactiver « Pages de même format, contenu centré » à l'export |
| Suppression du fond : une reproduction de document (manuscrit, gravure) de la couleur du papier et sans cadre net n'est pas reconnue comme image | Son fond devient blanc : l'écriture reste, le cadre et la teinte de la reproduction disparaissent (constaté sur une capture basse résolution) | curseur « Suppression du fond » à 0 pour cette page, ou réglage du livre désactivé |
| Texte imprimé très clair (gris pâle, plus clair que 62 à 88 % du papier) | Pâli, voire effacé, par la suppression du fond | baisser le curseur « Suppression du fond » pour la page |
| Centrage du contenu : un numéro de page ou une note isolée compte dans le bloc de contenu | Le bloc centré peut être un peu décalé par rapport au texte courant | sans effet sur le texte ; recadrage manuel si besoin |
| Couverture sombre : les petites bandes de couleur imprimées tout en haut (environ 2 % de la hauteur) restent hors du cadre | Bandes coupées sur la photo 00019 | recadrage manuel de la page (étape 4) |
| Détection des pages sombres calibrée sur le banc actuel (fond noir neutre, page en vis-à-vis claire) | Sur un autre fond, une page sombre peut rester non détectée : photo entière gardée et page « à vérifier » | recadrage manuel ; ajouter une photo de ce cas aux tests |
| Type de page détecté par la part d'images (≥ 3 % : avec image ; ≥ 60 % ou pas de papier clair : couverture) | Un encadré coloré compte comme « avec image » ; une page de titre sur aplat de couleur sans image compte comme « texte seul » | corriger le type dans « Retouche d'une page » (pris en compte dans les statistiques et les prix) |
| La corbeille des projets n'est jamais vidée automatiquement | Espace disque occupé par les projets supprimés | suppression définitive manuelle de `data/trash/projects/<dossier>` |
| Sélecteur de recadrage (souris) : interactions testées par la syntaxe JavaScript (Node) et par le traitement Python des coins, pas par un navigateur automatisé (Playwright absent) | Un défaut de glisser-déposer propre à un navigateur ne serait pas détecté par les tests | saisie numérique des coins dans la même fenêtre |
| Tableau détecté seulement s'il est réglé (traits de grille) ; un tableau sans traits (colonnes alignées) compte comme texte. Un graphique au trait sans aplat (courbes) compte comme texte seul | Prix de la page sous-estimé | corriger le type dans « Retoucher la page » |
