# Déploiement (Docker, CI/CD GitHub Actions, VPS)

## Image Docker

`Dockerfile` à plusieurs étapes :

- **`test`** : dépendances + code + tests. La CI y lance `pytest`, dans le même environnement que la production : Debian trixie, Python 3.14, Tesseract 5 (`fra`, `deu`, `eng`, `osd`), polices Liberation et DejaVu.
- **`runtime`** : application seule, sans les tests. Elle tourne avec l'utilisateur non root `app`, les données sont dans le volume `/data` (`BOOKOCR_DATA_DIR`), le port est 8501 et le contrôle de santé interroge `/_stcore/health`. Ollama y est désactivé par défaut.

```bash
docker build --target test -t ocr-numerisation:test . && docker run --rm ocr-numerisation:test   # tests
docker compose up -d --build                                                                    # application
```

LibreOffice n'est pas dans l'image : la conversion DOCX → PDF est facultative. Ses tests sont ignorés et le PDF direct (PyMuPDF) reste disponible.

## Configuration sur le serveur

Le fichier `.env` (facultatif), à côté de `docker-compose.yml`, n'est jamais versionné ni écrasé par le déploiement. Voir `.env.example`.

- **Ollama** : il n'est pas installé sur le VPS (vérifié le 20/10/2026), donc `OLLAMA_ENABLED=false` ; tout fonctionne avec OpenCV et Tesseract. Si Ollama est installé plus tard sur l'hôte, il faut le faire écouter sur l'interface Docker (`OLLAMA_HOST=0.0.0.0`) et mettre `OLLAMA_ENABLED=true`. `OLLAMA_BASE_URL` vaut par défaut `http://host.docker.internal:11434`.
- **Port** : `APP_PORT` (8501 par défaut). Les ports 80 et 443 du VPS sont déjà occupés par un autre service. Pour publier l'application en HTTPS, ajouter un proxy inverse vers `127.0.0.1:8501`.

## CI/CD (`.github/workflows/ci-cd.yml`)

| Job | Quand | Ce qu'il fait |
|---|---|---|
| `test` | chaque push sur `main` ou `audit-phase1`, chaque pull request vers `main`, lancement manuel | construit l'étape `test` et lance la suite de tests dans le conteneur ; rapport JUnit en artefact |
| `image` | push (pas les pull requests), après `test` | construit l'étape `runtime` et la publie sur `ghcr.io/<compte>/ocr` (étiquettes : branche, `sha-…`, version `v*`, `latest` sur `main`) |
| `deploy` | push ou lancement manuel, après `image` | copie `docker-compose.yml` et `deploy/deploy.sh` sur le VPS, se connecte à GHCR et lance `deploy/deploy.sh` ; ignoré si `VPS_HOST` n'est pas configuré |

Secrets GitHub (Settings › Secrets and variables › Actions) :

| Secret | Valeur |
|---|---|
| `VPS_HOST` | `157.173.103.147` |
| `VPS_USER` | `garrix` |
| `VPS_SSH_KEY` | clé privée d'une clé dédiée au déploiement, dont la clé publique est dans `~/.ssh/authorized_keys` du VPS (recommandé) |
| `VPS_PASSWORD` | à défaut de clé : mot de passe SSH (déconseillé) |
| `VPS_PATH` | facultatif, défaut `/home/garrix/ocr` |

Aucun secret n'est écrit dans le dépôt. `GITHUB_TOKEN` (fourni par GitHub) sert à publier l'image et, pendant le déploiement, à la télécharger sur le VPS.

## Mise à jour manuelle sur le VPS

```bash
cd /home/garrix/ocr
sh deploy/deploy.sh                                           # reconstruit l'image sur place
IMAGE=ghcr.io/<compte>/ocr:latest sh deploy/deploy.sh         # ou : image publiée par la CI
docker compose logs -f app                                    # journal
```

Les projets (photos, résultats, exports) sont dans le volume Docker `ocr-data`. Ils ne sont jamais supprimés par une mise à jour. Pour les sauvegarder :

```bash
docker run --rm -v ocr_ocr-data:/data -v "$PWD":/backup debian tar czf /backup/ocr-data.tgz /data
```
