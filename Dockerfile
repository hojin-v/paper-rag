FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md requirements-lock.txt ./
# src 전체가 아니라 hatchling이 빌드 대상으로 요구하는 최소 스텁만 먼저 만든다
# (pyproject.toml의 packages = ["src/paperrag"] 때문에 빈 디렉터리로는 안 됨). 실제
# 코드는 모든 pip install이 끝난 뒤(아래 COPY src ./src)에 넣는다 — 그래야 코드만
# 바뀌고 의존성(pyproject.toml/requirements-lock.txt)은 안 바뀐 배포에서 Docker가 아래
# pip install 레이어들을 캐시로 재사용한다. 원래는 COPY src ./src가 여기(첫 pip install
# 전)에 있었는데, 그러면 코드 한 줄만 바꿔도 이 레이어부터 캐시가 무효화돼 torch·
# paddlepaddle 등 무거운 의존성 전체가 매 배포마다 재설치됐다(2026-07-22 CD 빌드 시간
# 실측 후 발견).
RUN mkdir -p src/paperrag && touch src/paperrag/__init__.py
ARG PAPERRAG_EXTRAS="ocr,ui,worker,embed"
# embedder/api/worker/ui 4개 서비스가 이 Dockerfile 하나로 빌드한 이미지를 공유한다
# (docker-compose.yml, 전부 같은 image: paperrag:$TAG). embedder 서비스만 실제로
# sentence-transformers(embed extra)를 쓰지만, 서비스별로 다른 extras를 주면 같은
# 이미지 태그에 마지막으로 빌드된 서비스 것만 남아 덮어써지므로 여기서 항상 함께
# 설치한다 — 이미지 자체는 하나뿐이라(4개 컨테이너가 같은 이미지를 참조) 디스크에
# 중복 저장되지 않는다.
#
# requirements-lock.txt를 제약(-c)으로 걸어, 락파일에 있는 패키지는 그 버전 그대로
# 설치되게 한다(-c는 pyproject.toml이 실제로 요구하는 패키지만 제약할 뿐, 락파일에
# 없는 패키지를 새로 설치하지 않는다). ocr extra의 paddlepaddle/paddleocr는 이미
# pyproject.toml에 정확한 버전으로 고정돼 있어 이 락파일에는 없다(requirements-lock.txt
# 상단 주석 참고).
#
# 2026-07-22 실제 배포 시도에서 처음 발견: ocr extra의 kiwipiepy가 의존하는
# kiwipiepy_model은 PyPI에 wheel을 전혀 배포하지 않고 항상 sdist로만 배포되며,
# 그 setup.py가 빌드 시점에 numpy(및 setuptools — python:3.12-slim 기본 환경에는
# 둘 다 없음)를 직접 import한다(PEP 517 build-system.requires에 선언돼 있지 않은
# 그 패키지 자체의 결함) — pip의 기본 격리 빌드 환경에는 이게 없어
# "ModuleNotFoundError"로 실패한다. numpy·setuptools·wheel을 먼저 설치해 두고
# kiwipiepy만 --no-build-isolation으로 별도 설치해 그 환경을 보게 한다.
# --no-build-isolation을 전체 설치(마지막 줄)에 걸면 paperrag 자체의 빌드 백엔드
# (hatchling)가 격리 환경 밖이라 없어서 실패하므로, kiwipiepy 설치만 분리해 그
# 부작용을 피한다.
RUN pip install --no-cache-dir -c requirements-lock.txt numpy
RUN pip install --no-cache-dir setuptools wheel
RUN pip install --no-cache-dir --no-build-isolation kiwipiepy
#
# 2026-07-22: embed extra(sentence-transformers)를 위처럼 항상 설치하면서 torch가 딸려
# 들어오는데, PyPI 기본 torch 배포판은 이 서버(GPU 없는 단일 맥북, CPU 전용 —
# embed/encoder.py의 SentenceTransformer(device="cpu") 참고)에서 전혀 쓰지 않는
# nvidia-*-cu12/triton 등 CUDA 런타임을 통째로 딸려 보낸다(실측: pip install 로그에
# nvidia-cublas-cu12·nvidia-cudnn-cu12 등 다운로드로 수 GB, 빌드 시간 대부분이 여기서
# 소모됨). PyTorch가 CUDA 없이 배포하는 전용 인덱스(download.pytorch.org/whl/cpu)에서
# "+cpu" 빌드를 먼저 설치해 두면, 다음 줄의 전체 설치가 이미 만족된 torch를 다시
# 내려받지 않는다. --extra-index-url(← --index-url 아님)을 써서 torch 자체는 이
# 인덱스에서, filelock/sympy 같은 순수 파이썬 의존성은 그대로 PyPI에서 받게 한다.
RUN pip install --no-cache-dir --extra-index-url https://download.pytorch.org/whl/cpu torch==2.8.0+cpu
RUN pip install --no-cache-dir -c requirements-lock.txt ".[${PAPERRAG_EXTRAS}]"

# 실제 코드. 여기서부터는 코드가 바뀔 때마다 캐시가 무효화되지만, 위의 무거운 pip
# install 레이어들은 그대로 캐시에 남는다. paperrag 자체는 스텁으로 이미 설치돼 있으니
# 실제 소스로 갱신만 한다(의존성은 이미 설치됐으므로 --no-deps로 재해석 생략).
COPY src ./src
RUN pip install --no-cache-dir --no-deps --force-reinstall .

COPY db ./db
COPY scripts ./scripts
