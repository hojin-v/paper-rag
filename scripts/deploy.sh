#!/usr/bin/env bash
# paper-rag 배포 스크립트 — CD 워크플로우(.github/workflows/deploy.yml)와 수동
# `make deploy` 양쪽에서 호출한다. 온프레미스 단일 서버(맥북)에서 실행되며,
# 소스는 git, 배포 산출물은 git SHA로 태그된 단일 Docker 이미지다.
#
# 흐름: (커밋 동기화 →) 이미지 빌드 → 앱 중지 → 마이그레이션 → 새 이미지로 재기동
#       → 헬스체크 → 실패 시 이전 태그로 롤백 → 성공 시 오래된 이미지 정리.
#
# 안전 순서 주의: 컬럼 삭제 같은 파괴적 마이그레이션과 구 코드가 겹치면 안 되므로,
# 앱 컨테이너를 먼저 멈춘 뒤 마이그레이션을 적용한다(그동안 짧은 다운타임 발생).
# postgres/redis/ollama는 마이그레이션이 붙어야 하므로 멈추지 않는다.
#
# 한계(문서 15 참고): 롤백은 코드(이미지)만 이전 태그로 되돌린다. 이미 적용된 DB
# 마이그레이션은 자동으로 되돌리지 않으므로, 파괴적 스키마 변경은 가급적 하위 호환으로
# 만들고, 잘못된 마이그레이션은 사람이 직접 DB를 조치해야 한다.
set -euo pipefail

# 배포 대상 디렉터리: .env, data/, models/, docker-compose.yml이 있는 정식 프로젝트 경로.
# CD 러너의 임시 체크아웃이 아니라 이 경로에서 빌드·기동해야 볼륨·환경이 일관된다.
DEPLOY_DIR="${PAPERRAG_DEPLOY_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
STATE_DIR="$DEPLOY_DIR/.deploy"
CURRENT_FILE="$STATE_DIR/current_tag"
KEEP_IMAGES="${PAPERRAG_KEEP_IMAGES:-5}"       # 롤백용으로 보관할 최근 이미지 개수
APP_SERVICES="embedder api worker ui"

log() { printf '\033[1;34m[deploy]\033[0m %s\n' "$*"; }

cd "$DEPLOY_DIR"
mkdir -p "$STATE_DIR"

# CD에서 PAPERRAG_DEPLOY_REF(배포할 커밋 SHA)를 주면 정식 디렉터리를 그 커밋으로 맞춘다.
# 수동 실행 시에는 생략하고 현재 체크아웃된 HEAD를 그대로 배포한다.
if [ -n "${PAPERRAG_DEPLOY_REF:-}" ]; then
  log "커밋 동기화: $PAPERRAG_DEPLOY_REF"
  git fetch --quiet origin
  git checkout --quiet --force "$PAPERRAG_DEPLOY_REF"
fi

SHA="$(git rev-parse --short HEAD)"
PREV_TAG="$(cat "$CURRENT_FILE" 2>/dev/null || true)"
export PAPERRAG_TAG="$SHA"
log "배포 대상 커밋: $SHA (이전 배포: ${PREV_TAG:-없음})"

# 1. 테스트 게이트. CD는 ci job이 이미 통과시켰으므로 SKIP_TESTS=1로 건너뛴다.
#    수동 `make deploy`에서는 배포 직전 회귀를 한 번 더 막기 위해 기본 실행한다.
if [ "${SKIP_TESTS:-0}" != "1" ]; then
  log "테스트 실행 (ruff + pytest)"
  ruff check src tests
  pytest -q
fi

# 2. 이미지 빌드(SHA 태그). 이 단계는 구 앱이 아직 떠 있는 동안 진행돼 다운타임을 줄인다.
#    worker/ui는 docker-compose.yml에서 profiles가 붙어 있어(선택적 기동 대상) 서비스명을
#    명시하지 않으면 build/up 대상에서 조용히 빠진다 — $APP_SERVICES로 항상 명시한다.
log "이미지 빌드: paperrag:$SHA"
docker compose build $APP_SERVICES

# 3. 앱 중지 → 마이그레이션 → 재기동
log "앱 서비스 중지: $APP_SERVICES"
docker compose stop $APP_SERVICES || true
log "DB 마이그레이션 적용"
docker compose run --rm --no-deps api python scripts/apply_migrations.py
log "새 이미지로 재기동"
docker compose up -d $APP_SERVICES

# 4. 헬스체크: 새 코드가 실제로 떠서 응답하는지 확인(프로세스 기동 성공 여부 게이트).
log "헬스체크 대기 (/health)"
ok=""
for _ in $(seq 1 30); do
  if curl -fsS http://localhost:8000/health >/dev/null 2>&1; then ok=1; break; fi
  sleep 2
done

# 5. 실패 시 이전 이미지로 롤백
if [ -z "$ok" ]; then
  log "헬스체크 실패"
  if [ -n "$PREV_TAG" ]; then
    log "이전 이미지로 롤백: paperrag:$PREV_TAG"
    PAPERRAG_TAG="$PREV_TAG" docker compose up -d $APP_SERVICES || true
    log "롤백 완료. 새 마이그레이션은 자동으로 되돌리지 않으므로 DB 상태를 확인하세요."
  else
    log "롤백할 이전 이미지가 없습니다(최초 배포). 로그를 확인하세요: docker compose logs api"
  fi
  exit 1
fi

# 6. 성공 기록 + 오래된 이미지 정리(최근 KEEP_IMAGES개만 남김)
echo "$SHA" > "$CURRENT_FILE"
log "배포 성공: paperrag:$SHA"
docker images paperrag --format '{{.Tag}}' \
  | grep -vx latest \
  | grep -vx '<none>' \
  | tail -n +"$((KEEP_IMAGES + 1))" \
  | while read -r tag; do
      [ "$tag" = "$SHA" ] && continue
      log "오래된 이미지 삭제: paperrag:$tag"
      docker rmi "paperrag:$tag" >/dev/null 2>&1 || true
    done
docker image prune -f >/dev/null 2>&1 || true

log "현재 준비 상태(/ready):"
curl -fsS http://localhost:8000/ready 2>/dev/null | python3 -m json.tool 2>/dev/null || \
  log "  /ready 확인 실패 — 임베딩·Ollama·모델 준비 상태를 점검하세요."
