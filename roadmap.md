# Feuille de route

Chaque phase a un objectif et des critères d'acceptation **vérifiables**. Le détail des tâches est dans [planning.md](planning.md). États : `TODO`, `IN_PROGRESS`, `BLOCKED`, `DONE`.

| Phase | Objectif | Critères d'acceptation | État |
|---|---|---|---|
| P0. Organisation | Configuration Claude Code, règles, architecture, contrats, plan | Agents et skills reconnus par Claude Code ; scripts de diagnostic exécutés ; ARCHITECTURE et TECHNICAL_DECISIONS écrits | DONE |
| P1. Fondations | Dépendances propres, `config.py`, `models/`, pages synthétiques, squelette `pytest` | `pip install -r requirements.txt` réussit ; `test_models.py` passe (aller-retour JSON) ; les fixtures produisent les trois cas | TODO |
| P2. Import, aperçu, sauvegarde | Projets, import multiple (JPG/PNG/TIFF/PDF), vignettes, ordre, remplacement, suppression, rotation manuelle, historique ; tableau de bord et gestion des pages | Projet rouvert dans un nouveau processus identique ; originaux inchangés (SHA-256) ; écrans AppTest sans exception | TODO |
| P3. Correction géométrique et image | Orientation auto, inclinaison, feuille, perspective, ombres, courbure prudente ; réglages d'amélioration ; avant/après | Rotations 90/180/270 détectées ; page droite non modifiée ; réglages neutres = identité ; avertissement si incertain | TODO |
| P4. OCR et mise en page | OCR multilingue avec cache, faible confiance, blocs typés, colonnes, ordre, catégories A/B/C, illustrations extraites, format détecté | Similarité ≥ 0,9 sur les pages synthétiques ; ordre deux colonnes correct ; trois catégories correctes ; images extraites sans perte | TODO |
| P5. Éditeur et typographie | Éditeur de blocs (texte, styles, position, taille, ajout et suppression), légendes, images ; typographie globale ou par sélection ; contrôle (confiance, liste à vérifier, comparaison, restauration) | Chaque contrôle affiché a un effet vérifié dans le projet rechargé ; styles individuels préservés | TODO |
| P6. Composition et fonds | `composition_service` pour les trois modes, fonds reconstruits, aperçu = rendu PDF | Composition déterministe ; aperçu identique au PDF exporté ; débordements signalés | TODO |
| P7. Export DOCX | DOCX trois modes, styles réutilisables, sections par page, images, fonds, en-têtes et pieds | DOCX relu : texte en paragraphes, images, une section par page, pas d'image pleine page en mode modifiable | TODO |
| P8. Export PDF | PDF direct, fac-similé recherchable, conversion LibreOffice, validation, aperçu avant téléchargement | Nombre de pages exact, texte extractible, images, aucune page blanche ; conversion vérifiée si LibreOffice est présent | TODO |
| P9. Validation de bout en bout | Trois cas et photos réelles, performances, robustesse | `test_integration.py` passe ; mesures dans TEST_REPORT ; problèmes classés | TODO |
| P10. Documentation et livraison | README, guide utilisateur, dépannage, limites, CHANGELOG | Chaque commande du README exécutée ; installation vérifiée dans un venv neuf | TODO |
