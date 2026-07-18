FROM python:3.11-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
RUN pip install --upgrade pip && pip install -e ".[llm_extra,output]"

COPY app ./app
COPY assets ./assets
COPY alembic ./alembic
COPY alembic.ini ./
COPY docker-entrypoint.sh ./
RUN chmod +x docker-entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["./docker-entrypoint.sh"]
CMD ["app"]
