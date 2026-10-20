# syntax=docker/dockerfile:1
# Reconstruction de livres / numérisation intelligente — image Streamlit.
#   test    : image + tests (la CI lance la suite de tests dans le même environnement que la production)
#   runtime : image de production (sans les tests), utilisateur non root, données dans /data

ARG PYTHON_VERSION=3.14

# --------------------------------------------------------------------------- base commune
FROM python:${PYTHON_VERSION}-slim-trixie AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Tesseract 5 + langues (français, allemand, anglais, orientation), polices Liberation et DejaVu
# (export PDF/DOCX et pages de test), curl pour le contrôle de santé.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tesseract-ocr tesseract-ocr-fra tesseract-ocr-deu tesseract-ocr-eng tesseract-ocr-osd \
        fonts-liberation fonts-dejavu-core libglib2.0-0 curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install -r requirements.txt

# --------------------------------------------------------------------------- tests
FROM base AS test
COPY . .
# .env local ignoré, aucun appel au vrai Ollama (marqueur « ollama » désactivé par défaut)
ENV BOOKOCR_SKIP_DOTENV=1
CMD ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "--junitxml=/tmp/junit.xml"]

# --------------------------------------------------------------------------- production
FROM base AS runtime

ARG APP_VERSION=dev
LABEL org.opencontainers.image.title="ocr-numerisation" \
      org.opencontainers.image.description="Numérisation intelligente de livres photographiés (PDF recherchable, DOCX)" \
      org.opencontainers.image.version="${APP_VERSION}"

RUN useradd --create-home --uid 1000 app && mkdir -p /data && chown app:app /data
COPY --chown=app:app app.py config.py ./
COPY --chown=app:app .streamlit ./.streamlit
COPY --chown=app:app components ./components
COPY --chown=app:app models ./models
COPY --chown=app:app repositories ./repositories
COPY --chown=app:app services ./services
COPY --chown=app:app workers ./workers

ENV BOOKOCR_DATA_DIR=/data \
    OLLAMA_ENABLED=false \
    STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false
USER app
VOLUME ["/data"]
EXPOSE 8501
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS http://localhost:8501/_stcore/health || exit 1
CMD ["streamlit", "run", "app.py", "--server.port=8501", "--server.address=0.0.0.0"]
