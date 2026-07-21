# 15. CI/CD 배포 (완전 자동)

개발 노트북에서 `git push`하면, 클라우드에서 테스트가 돌고 통과 시 온프레미스 맥북(서버)이
자동으로 새 이미지를 빌드·배포한다. 서버에 직접 SSH로 들어가 파일을 고치지 않는다.

```
[개발 노트북(WSL)]  git push main
        │
        ▼
[GitHub 클라우드]   test job (ci.yml 재사용): ruff + pytest      ── 실패 시 배포 중단
        │  needs: test 통과
        ▼
[맥북 셀프호스트 러너]  deploy job → scripts/deploy.sh
        ├─ 정식 디렉터리를 이 커밋으로 동기화
        ├─ docker compose build            (paperrag:<git-sha>)
        ├─ 앱 중지 → migrate → up -d        (파괴적 마이그레이션 안전 순서)
        ├─ /health 헬스체크
        ├─ 실패 → 이전 이미지 태그로 롤백
        └─ 성공 → 최근 5개 이미지만 남기고 정리
```

# 1단계: 왜 이 구조인가

맥북은 Tailscale 뒤에 있어 GitHub 클라우드에서 직접 접속할 수 없다. 그래서 역할을 나눈다.

| 역할 | 실행 위치 | 하는 일 |
| --- | --- | --- |
| CI (test/lint) | GitHub 클라우드 러너 | push/PR마다 ruff·pytest. 서버 접근 불필요 |
| CD (배포) | 맥북의 셀프호스트 러너 | 러너가 GitHub을 바깥으로 폴링(인바운드·시크릿 불필요)하다 job을 받아 맥북에서 직접 빌드·배포 |

레지스트리(GHCR) 경유 대신 맥북에서 직접 빌드하는 이유: Paddle이 Linux 전용이고 맥이 Apple
Silicon이라, 클라우드(amd64)에서 빌드해 내려받으면 아키텍처가 어긋난다. 맥이 자기 아키텍처로
빌드하면 이 문제가 없다.

# 2단계: 맥북에 셀프호스트 러너 설치 (1회)

GitHub 저장소 → **Settings → Actions → Runners → New self-hosted runner → macOS**가 주는
명령을 그대로 실행한다(다운로드 → `./config.sh` → 서비스 등록). 설정 시 유의점:

| 항목 | 값 |
| --- | --- |
| 라벨(labels) | `paperrag` 추가 (deploy.yml의 `runs-on: [self-hosted, paperrag]`와 일치해야 함) |
| 실행 사용자 | Docker·정식 프로젝트 경로에 접근 가능한 사용자(현재 개발 계정) |
| 상주 방식 | `./svc.sh install && ./svc.sh start` (launchd 서비스로 등록해 재부팅 후에도 자동 기동) |

```bash
# 러너가 docker를 찾을 수 있어야 한다(Docker Desktop 실행 중 + PATH에 docker).
docker version && docker compose version
```

검증: 저장소 Settings → Actions → Runners에 러너가 **Idle**로 보이면 성공.

# 3단계: GitHub 저장소 설정

| 종류 | 이름 | 값 | 용도 |
| --- | --- | --- | --- |
| Variable | `PAPERRAG_DEPLOY_DIR` | 맥북의 정식 프로젝트 경로 (예: `/Users/hojin/Projects/paper-rag`) | 배포 스크립트가 빌드·기동할 디렉터리(.env·data·models가 있는 곳) |

Settings → Secrets and variables → Actions → **Variables** 탭에서 추가한다. 비밀값이 아니므로
Secret이 아니라 Variable로 둔다.

> 이 경로는 러너의 임시 체크아웃이 아니라 **실제 서비스 디렉터리**여야 한다. 배포 스크립트가
> 그 경로를 배포 대상 커밋으로 `git checkout --force` 동기화한 뒤 빌드한다(추적되지 않는
> `.env`·`data/`·`models/`는 건드리지 않는다).

# 4단계: 최초 배포 (cutover, 1회성)

지금까지 손으로 띄운 ad-hoc 컨테이너를 compose 관리로 전환하는 한 번만 하는 작업이다. 검수
라벨링이 진행 중이 아닐 때(짧은 다운타임 허용 가능한 시점) 수행한다.

```bash
cd "$PAPERRAG_DEPLOY_DIR"
git pull

# 1) 손으로 띄운 ad-hoc 컨테이너 정리 (데이터 볼륨 pgdata·바인드 data/review는 보존됨)
docker rm -f paper-rag-api paper-rag-worker paper-rag-postgres 2>/dev/null || true

# 2) compose로 최초 배포 (이미지 빌드 + 전체 기동 + 마이그레이션)
make deploy

# 3) 파일로 저장돼 있던 검수 문서를 review_documents 테이블로 이전 (PostgresReviewStore 전환)
docker compose run --rm --no-deps api python scripts/backfill_review_documents.py
```

검증:
```bash
curl -s http://localhost:8000/ready | python3 -m json.tool      # status: ready
docker compose ps                                               # 서비스가 compose 관리 하에 Up
```

> 주의: compose의 postgres는 기존과 같은 `pgdata` 볼륨을 재사용하고 호스트 5433에 매핑되므로
> DB 데이터는 그대로다. 검수 PDF·이미지는 `./data/review` 바인드 마운트로 보존된다. 다만 새
> 코드는 검수 메타데이터를 파일이 아니라 DB에서 읽으므로(3)의 backfill을 반드시 실행한다.

# 5단계: 평상시 배포 (자동)

이후로는 개발 노트북에서 push만 하면 된다.

```bash
# 개발 노트북(WSL)
make test lint      # 로컬 사전 확인(선택)
git push origin main
```

push하면 GitHub Actions가 test job(클라우드) → deploy job(맥북 러너)을 자동 실행한다. 진행
상황은 저장소 **Actions** 탭에서 본다. `concurrency`로 배포가 겹치지 않게 직렬화된다.

# 6단계: 수동 배포·롤백

러너 없이 맥북에서 직접 배포하거나 급히 롤백해야 할 때:

| 작업 | 명령 |
| --- | --- |
| 현재 커밋 수동 배포 | `make deploy` (배포 전 ruff·pytest 자동 실행) |
| 특정 커밋으로 롤백 | `git checkout <sha> && make deploy` |
| 직전 배포 태그 확인 | `cat .deploy/current_tag` |

배포 스크립트는 헬스체크(`/health`) 실패 시 자동으로 직전 이미지 태그로 되돌린다. 수동
롤백도 같은 방식(이전 커밋 체크아웃 후 재배포)이다.

# 7단계: 이미지 보관·정리

배포마다 `paperrag:<git-sha>` 이미지가 새로 생긴다. 관리하지 않으면 계속 쌓여 디스크를
채운다(Paddle 포함 이미지는 1.5GB+). 배포 스크립트가 매 성공 후 자동 정리한다.

| 항목 | 동작 |
| --- | --- |
| 보관 개수 | 최근 `PAPERRAG_KEEP_IMAGES`개(기본 5) `paperrag:<sha>` 이미지만 남김 |
| 오래된 이미지 | 그보다 오래된 태그는 `docker rmi`로 삭제 |
| dangling 레이어 | `docker image prune -f`로 정리 |
| 롤백 여력 | 보관된 최근 5개 태그 범위 안에서 즉시 롤백 가능 |

레이어 캐싱으로 베이스 이미지 레이어는 공유되므로 5개가 5×1.5GB는 아니다.

# 8단계: 알아둘 한계

- **마이그레이션은 롤백되지 않는다.** 배포 스크립트의 자동 롤백은 코드(이미지)만 이전 태그로
  되돌린다. 이미 적용된 DB 마이그레이션은 그대로 남으므로, 파괴적 스키마 변경(컬럼 삭제 등)은
  가급적 하위 호환으로 만들고, 잘못된 마이그레이션은 사람이 직접 DB를 조치한다.
- **단일 서버 완전 자동 CD의 위험.** 서버가 하나뿐이라 나쁜 커밋이 main에 들어가면 롤백 전까지
  유일한 라이브 서버가 영향받는다. 안전망은 CI 게이트 + 헬스체크 + 자동 롤백이다. 더 보수적으로
  가려면 deploy job에 `environment: production`을 추가하고 그 environment에 required reviewers를
  걸어 **배포 직전 수동 승인**을 요구하는 Continuous Delivery로 바꾼다.
- **배포 중 짧은 다운타임.** 앱 중지 → 마이그레이션 → 재기동 구간 동안(수 초~수십 초) API가
  응답하지 않는다. 무중단이 필요해지면 blue-green으로 확장한다(현재 범위 밖).

## 완료 체크리스트

- [ ] 맥북에 `paperrag` 라벨의 셀프호스트 러너가 Idle 상태로 등록됐다.
- [ ] 저장소 Variable `PAPERRAG_DEPLOY_DIR`을 설정했다.
- [ ] 최초 cutover로 ad-hoc 컨테이너를 compose 관리로 전환하고 backfill을 실행했다.
- [ ] `git push origin main` 시 Actions에서 test → deploy가 자동 실행된다.
- [ ] `/ready`가 배포 후 `status: ready`를 반환한다.
- [ ] `make deploy`로 수동 배포·롤백이 가능함을 확인했다.
- [ ] 오래된 이미지가 최근 5개만 남고 자동 정리됨을 확인했다.
