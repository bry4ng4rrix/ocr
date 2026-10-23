# Changelog

Format inspiré de *Keep a Changelog*. Dates au format AAAA-MM-JJ.

## [Non publié]

### Modifié — 2026-10-23 (boutons regroupés par objectif)
- Numérisation intelligente (navigation latérale et six étapes inchangées) :
  - étape 1 : les options de traitement (OCR, fond, Gemma) sont réunies ; « Centrer le contenu » rejoint la mise en page du PDF (étape 5) ;
  - étape 3 : affichage galerie ou tableau, filtre et « Valider toutes les pages » sur une ligne ; le tableau des pages devient une vue des résultats ;
  - étape 4 : « Retoucher cette page » et « Modifier toutes les pages… » côte à côte, chacun dans une fenêtre ;
  - étape 5 : « Mise en page » puis « Options d'export », puis création et téléchargement.
- Fenêtre de retouche : chaque outil porte ses boutons (détection des bords dans Recadrage ; amélioration automatique, original et réglages précédents dans Réglages ; analyse dans Analyse Gemma ; traiter et valider dans Aperçu).
- Aucune fonctionnalité retirée.

### Ajouté — 2026-10-23 (format de sortie, orientation, modifications de toutes les pages)
- Format des pages du PDF (étape 5) :
  - taille de chaque page ;
  - format commun du livre ;
  - A3, A4, A5, B5, Letter, Legal ou personnalisé (mm) ;
  - orientation automatique (chaque page suit le sens de sa photo), portrait ou paysage ;
  - marge en mm ;
  - la photo est ajustée sans déformation ni recompression, la couche de texte suit. Réglages enregistrés par livre (l'ancien `uniform_pages` est converti).
- « Modifier toutes les pages » (étape 4), pour toutes les pages ou les pages à vérifier, retraitées ensuite en arrière-plan avec un historique par page :
  - recadrage commun à la souris sur une page de référence, ou retour au contour automatique ;
  - rognage supplémentaire des bords (`extra_*`), qui s'ajoute au rognage automatique et reste en place quand la page est retraitée ;
  - rotation.
- Rognage d'un bord porté à 15 % au plus.

### Ajouté et modifié — 2026-10-23 (validation en bloc, PDF en un clic, OCR facultatif)
- Numérisation intelligente :
  - bouton « Valider toutes les pages » : valide les pages dont l'image et le texte sont à jour ; les pages non prêtes sont listées (`scanning_service.validate_all`) ;
  - un seul bouton « Créer le PDF » : le PDF est enregistré dans le projet, puis le téléchargement s'affiche. Au retour sur l'écran, le PDF enregistré est retrouvé ; il est proposé tant qu'il est à jour (empreinte des pages, images, OCR et options), sinon « Mettre à jour le PDF » ;
  - le fichier n'est lu qu'au clic sur « Télécharger » (pas de 100 Mo envoyés à chaque rafraîchissement).
- PDF créé et enregistré automatiquement à la fin d'un traitement en arrière-plan si toutes les pages sont prêtes (réglage du livre `auto_export`, activé par défaut).
- Options du PDF enregistrées avec le livre : format uniforme, pages à vérifier incluses.
- Réglage « Reconnaître le texte (OCR) » à l'étape 1 (`run_ocr`). Désactivé : traitement plus rapide, PDF sans couche texte. Réactivé : les pages concernées sont signalées « à retraiter ».
- Les 3 derniers PDF de numérisation sont gardés par livre (`BOOKOCR_SCAN_KEEP_EXPORTS`) ; les plus anciens, recréables, sont effacés.

### Corrigé — 2026-10-23
- Deux exports créés dans la même seconde s'écrasaient (nom horodaté à la seconde) : un suffixe `-2`, `-3`… rend désormais le nom unique (DOCX et PDF).
- Le dernier export affiché n'était pas lié au livre (mémoire de session) : il est maintenant relu dans le dossier `exports/` du livre.
- Changer « Supprimer le fond » ou « Centrer le contenu » remettait les autres réglages du livre à leur valeur par défaut.

### Ajouté — 2026-10-20 (Docker et CI/CD)
- `Dockerfile` à deux cibles :
  - `test` : suite de tests dans l'environnement de production ;
  - `runtime` : Python 3.14, Tesseract `fra`/`deu`/`eng`/`osd`, polices Liberation et DejaVu, utilisateur non root, volume `/data`, contrôle de santé.
- `.dockerignore` (données privées, `.env` et environnements exclus de l'image), `docker-compose.yml`, `deploy/deploy.sh` (mise à jour et attente du contrôle de santé).
- Workflow `.github/workflows/ci-cd.yml` : tests, puis image publiée sur GHCR, puis déploiement SSH sur le VPS. Les secrets sont lus dans GitHub et aucun n'est dans le dépôt. Documentation : `docs/DEPLOYMENT.md`.
- Tests portables : les polices de test sont cherchées par nom de fichier dans `/usr/share/fonts` (chemins Arch et Debian/Ubuntu).

### Ajouté — 2026-10-19 (pages avec tableau)
- Nouveau type de page « Avec tableau(x) » avec son prix (onglet Prix, bilan de la numérisation). Détection (`scanning_service.has_table`) : au moins 3 traits fins horizontaux et 3 verticaux, avec au moins 9 croisements ; les aplats et les photos sont exclus. Ordre de priorité : couverture, tableau, image, texte seul.
- Les pages déjà classées sont reclassées automatiquement (`CLASSIFIER_VERSION = 2`), sauf celles dont le type a été choisi à la main. Un ancien `pricing.json` sans prix de tableau se charge (prix 0).

### Corrigé et ajouté — 2026-10-19 (retouche d'une page)
- Le bouton « Retoucher cette page » de la galerie semblait sans effet : la page était sélectionnée tout en bas de l'écran, hors de vue. La retouche s'ouvre désormais dans une fenêtre sur la page cliquée. Elle reste ouverte après chaque action, avec boutons page précédente/suivante, et propose cinq outils : aperçu et commandes, recadrage, réglages, orientation et type, analyse Gemma.
- Recadrage manuel en mode sélection (`components/crop_selector.py`, composant Streamlit v2) :
  - rectangle tracé à la souris, ou 4 coins libres pour une page en perspective ;
  - extérieur assombri, grille des tiers, flèches du clavier ;
  - « Image entière », « Contour proposé » et « Appliquer le recadrage » (contour validé par `scanning_service.set_manual_quad`) ;
  - saisie numérique en repli.

### Ajouté — 2026-10-19 (gestion des projets, statistiques et prix)
- Tableau de bord :
  - modifier un projet (nom, auteur, pages attendues, langues), via `project_service.update_project_details` ;
  - le mettre à la corbeille (déplacement dans `data/trash/projects/`, rien n'est effacé ; refusé pendant un traitement) ;
  - le restaurer depuis la corbeille. L'état de la file de numérisation est reconstruit d'après les résultats enregistrés.
- Type de chaque page numérisée : texte seul, avec image(s) ou couverture avec image (`scanning_service.classify_page`). Il est détecté sur l'image finale et corrigible à la main dans « Retouche d'une page ».
- Numérisation intelligente, section 6 « Statistiques et prix » : pages terminées par type, montant par type, total des pages et total des prix.
- Onglet « Prix » (`components/screens/pricing.py`, `services/pricing_service.py`, `data/pricing.json`) : prix par type de page, monnaie, montant par livre et pour tous les livres.

### Corrigé — 2026-10-19 (cadrage des pages sombres)
- Une couverture ou une page sombre (photo pleine page) sur le banc noir n'était pas détectée : la photo entière était gardée. Ajout de `page_bounds.detect_line_quad`, troisième recours après les deux autres détecteurs. Il cherche une paire de bords haut et bas alignés, prend les bords latéraux sur les traits verticaux proches ou les relie entre les extrémités (bord invisible sur fond noir), et refuse un contour qui englobe du fond (bande intérieure sombre et neutre) ou qui n'est pas posé sur un fond sombre.
- Le secours « couleur du papier » est réservé aux papiers clairs. Pages sans papier clair : ni rognage des bords ni fausse alerte « bande sombre » ou « encre au bord ».

### Ajouté — 2026-10-19 (suppression du fond et centrage du contenu)
- **Suppression du fond**, réglage du livre activé par défaut, avec un curseur par page :
  - le papier devient blanc pur après égalisation de l'éclairage ;
  - le texte garde sa forme, son lissage et sa couleur (transition douce, pas de seuillage) ;
  - les images sont conservées : photos, illustrations, aplats colorés et rectangles encadrés (`image_enhancement.remove_background`, `image_regions`).
- **Contrôle qualité adapté** : le blanc du papier n'est plus compté comme un écrêtage ; la fidélité des couleurs est mesurée par la teinte, dans les images seulement.
- **Centrage du contenu dans le PDF** (réglage du livre, activé par défaut) : le bloc de texte et d'images (`image_quality_service.content_box`) est centré sur une page de format commun ; la couche OCR suit le décalage.
- Réglages enregistrés par livre (`scan/settings.json`) ; bouton « Retraiter tout le livre » après un changement (les réglages faits à la main sont conservés).

### Modifié — 2026-10-19 (numérisation intelligente, retours utilisateur)
- Écran réorganisé en 5 étapes. Un livre (existant ou nouveau) reçoit toutes ses photos en une fois, par téléversement ou depuis un dossier local, dans l'ordre naturel des noms. Un bouton « Importer et traiter le livre » lance le traitement. La galerie montre chaque page avant/après avec la liste des modifications faites. Viennent ensuite la retouche d'une page et le PDF du livre.
- Bords de page plus robustes (`services/page_bounds.py`) : secours par la couleur du papier quand la page n'est pas claire sur fond sombre (marge blanche, bandes noires, tranche du livre). Il n'est utilisé que si du fond entoure au moins 3 côtés et s'il ne contredit pas un contour existant.
- Bords restants retirés après redressement : bandes sombres, rangées sans papier, et tranches d'autres pages au-delà du vrai bord (ligne continue).
- Nettoyage du fond (`paper_whitening`) : éclairage du papier uniformisé et papier ramené vers le blanc ; photographies au gain du papier voisin, couleurs conservées.
- PDF « pages de même format, contenu centré » (par défaut) : format = taille médiane des pages + marge ; chaque photo ajustée sans déformation et centrée, sans recompression.

### Ajouté — 2026-10-19 (numérisation intelligente)
- Écran « Numérisation intelligente » (`components/screens/smart_scan.py`). Il réunit :
  - l'état réel d'Ollama et de Tesseract, la progression par livre et pour toute la bibliothèque ;
  - la sélection des pages (toutes, à vérifier, en erreur, en attente) ;
  - l'aperçu original / redressée / améliorée, la comparaison avant/après et le zoom 100 % ;
  - les commandes : traiter, analyser avec Gemma, détecter les bords, améliorer, restaurer, valider ;
  - la rotation et le recadrage manuels, des curseurs avec aperçu local (sans Gemma) et l'export du PDF du livre.
- PDF fidèle recherchable (`pdf_export.build_searchable_pdf`, `validate_searchable_pdf`) :
  - photographie finale insérée sans recompression ;
  - mots OCR en mode de rendu 3, alignés sur leur boîte ;
  - rapport de validation JSON (pages, texte, invisibilité, mots témoins, octets d'image, couleurs).
- Analyse visuelle Gemma 3 facultative (`image_analysis_service`, `ollama_service.chat_json`) :
  - schéma JSON imposé et validation Pydantic avec liste blanche d'actions ;
  - cache par empreinte et modèle, nouvelles tentatives bornées, une requête à la fois ;
  - aucun téléchargement de modèle.
- Mesures et contrôle avant/après (`image_quality_service`) ; plan de correction déterministe confirmé par les mesures, exposition, rognage des bandes de fond par bord (`image_enhancement`).
- Orchestration (`scanning_service`), file persistante SQLite `scan_tasks` (`repositories/scan_repository`), worker de reprise (`workers/scan_runner`).
- Configuration `OLLAMA_MAX_RETRIES`, `OLLAMA_ANALYSIS_ENABLED`, `OLLAMA_AUTO_APPLY`, `OLLAMA_MAX_PARALLEL_REQUESTS`, `BOOKOCR_SCAN_*` ; lecture d'un fichier `.env` local (ignoré par Git, désactivée pendant les tests).
- Documentation : `docs/gemma3-scanning.md`, ARCHITECTURE §8, quality-control, audit §9. Tests : `test_ollama_analysis`, `test_scan_quality`, `test_searchable_pdf`, `test_scanning_service`, `test_ollama_integration` (facultatif).

### Ajouté — 2026-10-19
- Écran « Numérisation et amélioration PDF » : import d'images/PDF, recadrage et séparation réglable, modes d'amélioration locaux, validation et réordonnancement par page, export PDF/ZIP ; analyse Gemma facultative via Ollama.
- Écran « Aperçu en direct » (`components/screens/live_preview.py`) : comparaison côte à côte temps réel avec à gauche l'image (originale ou corrigée avec surlignage optionnel des boîtes) et à droite le texte extrait sur une page blanche sans fond, avec sélecteur de mode (disposition positionnée fidèle ou flux de lecture structuré).
- Module de rendu pur `components/live_preview_render.py` (`render_positioned_html`, `render_flow_html`, `draw_overlay_boxes`) et tests associés (`tests/test_live_preview.py`).

### Corrigé — 2026-10-19
- Les pages sans bande de routage sont normalisées avant l'écriture JSON ; un aller-retour de sauvegarde conserve désormais l'égalité entre l'objet projet et le projet rouvert.

### Ajouté — 2026-10-16
- Organisation Claude Code : 7 sous-agents (`.claude/agents/`), 6 skills de savoir-faire et 7 commandes (`.claude/skills/`), dont 3 scripts de diagnostic (`ocr_probe.py`, `check_docx.py`, `check_pdf.py`).
- Règles du projet : `AGENTS.md` (source unique) importé par `CLAUDE.md`.
- Documentation : `docs/ARCHITECTURE.md` (contrats v1, diagramme Mermaid), `docs/TECHNICAL_DECISIONS.md`, `docs/TEST_STRATEGY.md`, `docs/KNOWN_LIMITATIONS.md`, `roadmap.md`, `planning.md`.

### Contexte
- L'application précédente (PDF seul, commit `ef66f25`) a été supprimée par l'utilisateur (commit `7760065`). Ses algorithmes servent de référence (TECHNICAL_DECISIONS D-01).
