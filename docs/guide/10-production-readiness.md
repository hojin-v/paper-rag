# 10. 실사용 준비와 공개 제약

테스트용 대체 결과를 운영 결과로 오인하지 않도록 실행 전 구성과 남은 위험을 확인한다.

```text
preflight
├─ 모든 PDF의 Paddle OCR 정책
├─ PyMuPDF·Paddle 런타임과 로컬 모델 3종
├─ 실제 BGE-M3 임베딩
├─ Ollama 지정 모델
├─ PostgreSQL
└─ 운영 폴백 차단
```

# 1단계: 설치와 모델 준비

```bash
source .venv/bin/activate
pip install -e ".[ingest-full]"
PYTHONPATH=src ./scripts/with_paddle_runtime.sh \
  .venv/bin/python scripts/download_paddle_models.py
```

다운로드 스크립트는 `Settings`의 모델명과 `PAPERRAG_PADDLEX_MODEL_SOURCE`를 사용해 다음 공식 추론
모델을 `PAPERRAG_PADDLEX_CACHE_DIR/official_models`에 준비한다.

| 역할 | 기본 모델 | 현재 로컬 크기 |
| --- | --- | ---: |
| 레이아웃 | `PP-DocLayout-M` | 23MB |
| 텍스트 검출 | `PP-OCRv5_mobile_det` | 4.8MB |
| 한국어·영어 텍스트 인식 | `korean_PP-OCRv5_mobile_rec` | 14MB |

Linux 네이티브 실행에는 `libgomp1`이 필요하다. 표준 설치는 OS 패키지 관리자를 사용한다.

```bash
sudo apt-get install libgomp1 libgl1 libglib2.0-0
```

현재 작업공간의 `scripts/with_paddle_runtime.sh`는 sudo 권한이 없었던 개발 머신에서 추출한
`.vendor/libgomp1`을 우선 사용하고 MKLDNN을 비활성화한다. Docker 이미지는 `Dockerfile`에서 시스템
패키지를 설치하므로 이 우회에 의존하지 않는다.

# 2단계: 운영 정책과 준비 상태 확인

| 설정 | 운영값 | 실패 처리 |
| --- | --- | --- |
| `PAPERRAG_RUNTIME_MODE` | `production` | 준비 실패 |
| `PAPERRAG_INGEST_BACKEND` | `paddle` | 준비 실패 |
| `PAPERRAG_REVIEW_DEFAULT_BACKEND` | `paddle` | 준비 실패 |
| `PAPERRAG_EMBED_ENCODER` | `st` | `hash`이면 준비 실패 |
| `PAPERRAG_ALLOW_DEGRADED_RESULTS` | `false` | LLM 실패를 정상 결과로 대체하지 않음 |
| `PAPERRAG_ALLOW_DIAGNOSTIC_BACKENDS` | `false` | simple/docling 운영 업로드 차단 |
| `PAPERRAG_PADDLE_ISOLATE_PROCESS` | `true` | 레이아웃·OCR 종료 후 모델 CPU·메모리 회수 |

```bash
PYTHONPATH=src ./scripts/with_paddle_runtime.sh \
  .venv/bin/python scripts/preflight.py
curl -s http://localhost:8000/ready | python -m json.tool
```

`/health`는 프로세스 생존만 나타낸다. `/ready`가 HTTP 200과 `status=ready`를 반환해야 논문을 처리할
구성요소가 준비된 것이다. 이 판정은 실제 논문 OCR 정확도와 production 처리량 합격을 의미하지 않는다.

# 3단계: 현재 검증된 구현 범위

| 기능 | 검증 수준 |
| --- | --- |
| 모든 PDF 페이지 이미지 렌더링 | 단위 테스트와 합성 PDF 실모델 실행 |
| 단계형 레이아웃→영역 OCR·좌표 환산 | Paddle 3.3.0 CPU, PaddleOCR 3.7.0 실제 제목 crop 실행 |
| 텍스트 검출 기반 레이아웃 자동 보정 | 새 논문 220개 텍스트 선, 초기 커버리지 90%, 21개 확장·5개 본문 추가 |
| Paddle 작업 중 API 응답성 | 별도 spawn 프로세스 실행 중 `/health` 0.44초, 종료 후 API RSS 약 126MB |
| 자동 품질 게이트 | OCR 영역 인식률, 제목 존재·인용문 일관성, 표 구조 OCR 성공 여부 판정 |
| 읽기 전용 자동 처리 품질 모니터와 관리자 교정 분리 | viewer 모드·API 실응답과 HTML 테스트 |
| 표 영역 텍스트 보존 | PP-LCNet 분류와 SLANeXt_wired/SLANet_plus 비교, Excel 셀 정규화 테스트 |
| 본문 필터·단락·요약·키워드 | 단위·통합 계약 테스트 |
| PostgreSQL+pgvector 저장 | 실제 DB 기동·마이그레이션과 저장소 테스트 |
| BGE-M3 HTTP 임베딩 | 실제 CPU 1024차원 추론과 HTTP health |
| Ollama | 지정 Qwen2.5-7B Q4 모델과 JSON 응답 |
| 검색·대표 1편+연관 1편·Excel | 단위·API 테스트 |
| 운영 대체값 차단 | `/ready`와 회귀 테스트 |
| Colab 학습데이터 내보내기 | ZIP 구조 테스트 |

# 4단계: 숨기지 않는 잔여 위험

| 위험 | 현재 영향 | production 전 조치 |
| --- | --- | --- |
| 실제 과학 논문 평가셋 없음 | mAP·CER·TEDS 합격을 주장할 수 없음 | 언어·조판별 정답 표본 구축 |
| 실제 LayoutLMv2 첫 페이지 제목·Abstract 누락 | 실모델 실행은 되지만 품질 합격 아님 | 실제 10편 검수 후 모델 크기·학습 결정 |
| wired/무선 분류가 DPI에 따라 바뀜 | 단일 분류 결과의 잘못된 모델 선택 가능 | 구조 밀도 기준 미달 시 양쪽 모델 비교 적용 완료, TEDS 정답 평가는 별도 필요 |
| 자동 제목 박스가 `Bengali` 앞부분을 제외 | 작은 박스에서 전체 제목을 얻던 모순은 제거됐으나 자동 결과는 불완전 | 레이아웃 단계에서 좌표 확장 후 OCR, 반복 시 레이아웃 학습 검토 |
| 읽기 순서 전용 편집 UI 없음 | 신규/기존 박스와 중복은 수정 가능하지만 임의 순서 교정은 숫자 기반 | 검수 빈도 측정 후 순서 편집기 구현 |
| 200 DPI 실제 2단 논문 첫 페이지 약 5분 30초 | 긴 PDF의 동기 HTTP 시간 초과·낮은 동시성 | 작업 제출·상태 조회·worker polling 연결 |
| FileReviewStore가 로컬 JSON 기반 | API 다중 replica 동시 수정 안전성 없음 | 단일 API 유지 또는 DB 저장소 전환 |
| CLI가 항상 STEP 1부터 재실행 | 장애 후 중복 적재 가능 | 체크포인트와 idempotency key 구현 |
| 논문 provenance가 JSONL manifest에만 있음 | 수집 파일 추적은 가능하지만 DB 결과와 자동 조인되지 않음 | provenance 테이블과 processing run 연결 |
| 모델·프롬프트 버전이 처리 레코드에 없음 | 결과 재현·교체 감사가 어려움 | processing run manifest 저장 |
| 인증·권한·TLS·악성 PDF 격리 없음 | 외부 공개 서비스로 배포 불가 | 내부 실증 후 보안 계층 추가 |

현재 `/ready`는 실제 구성으로 통과한다. 그래도 표현 가능한 단계는 “내부 단일 사용자 실사용 확인이 가능한
MVP”이며, 위 문제가 해결되기 전에는 “외부 공개 production 완료”라고 표현하지 않는다.

# 5단계: 실사용 판정

1. `/ready`가 200인지 확인한다.
2. 디지털 PDF 5편과 스캔 PDF 5편을 업로드한다.
3. 자동 품질 합격률과 예외 대기열 비율을 확인한다.
4. 합격 결과를 적재하고 한글·영문 질의로 검색한다.
5. 예외 문서 표본에서 레이아웃·OCR·표 실패 원인을 관리자가 확인한다.
6. Excel 메타데이터·원문·단락·요약·키워드가 화면 근거와 일치하는지 확인한다.
7. 실패 파일, 처리 시간과 오류가 기록되는지 확인한다.

## 완료 체크리스트

- [x] hash 임베딩이 운영 설정에서 차단된다.
- [x] simple/docling 업로드가 운영 설정에서 차단된다.
- [x] LLM 실패가 요약 성공으로 기록되지 않는다.
- [x] Paddle 패키지와 핵심 로컬 모델이 준비되어 있다.
- [x] `/ready`가 현재 구성요소를 `ok`로 보고한다.
- [ ] 실제 PDF 10편의 OCR·검색·Excel을 직접 확인했다.
- [ ] 품질·처리량 기준과 보안 조치를 충족했다.
