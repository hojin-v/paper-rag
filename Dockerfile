FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src ./src
ARG PAPERRAG_EXTRAS="ingest-full,ui,worker"
RUN pip install --no-cache-dir ".[${PAPERRAG_EXTRAS}]"

COPY db ./db
COPY scripts ./scripts
