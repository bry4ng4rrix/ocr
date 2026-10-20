#!/bin/sh
# Met à jour l'application sur le serveur (lancé par la CI, ou à la main dans le dossier de l'application).
#   IMAGE=ghcr.io/<compte>/ocr:<tag> sh deploy/deploy.sh   → image publiée par la CI
#   sh deploy/deploy.sh                                     → image construite sur place (docker compose build)
# Les données (volume ocr-data) et le fichier .env du serveur ne sont jamais touchés.
set -eu

cd "$(dirname "$0")/.."

if [ -n "${IMAGE:-}" ]; then
    export IMAGE
    docker compose pull app
    docker compose up -d --no-build app
else
    docker compose up -d --build app
fi

# Attendre que l'application réponde (contrôle de santé Streamlit), 2 minutes au plus.
i=0
until docker compose exec -T app curl -fsS http://localhost:8501/_stcore/health >/dev/null 2>&1; do
    i=$((i + 1))
    if [ "$i" -ge 60 ]; then
        echo "L'application ne répond pas après 2 minutes :" >&2
        docker compose logs --tail=50 app >&2
        exit 1
    fi
    sleep 2
done
docker image prune -f >/dev/null 2>&1 || true
echo "Application à jour et en bonne santé."
