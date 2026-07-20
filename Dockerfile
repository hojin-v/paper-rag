FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md requirements-lock.txt ./
COPY src ./src
ARG PAPERRAG_EXTRAS="ocr,ui,worker"
# requirements-lock.txt를 제약(-c)으로 걸어, 락파일에 있는 패키지는 그 버전 그대로
# 설치되게 한다(-c는 pyproject.toml이 실제로 요구하는 패키지만 제약할 뿐, 락파일에
# 없는 패키지를 새로 설치하지 않는다). ocr extra의 paddlepaddle/paddleocr는 이미
# pyproject.toml에 정확한 버전으로 고정돼 있어 이 락파일에는 없다(requirements-lock.txt
# 상단 주석 참고).
RUN pip install --no-cache-dir -c requirements-lock.txt ".[${PAPERRAG_EXTRAS}]"

COPY db ./db
COPY scripts ./scripts
