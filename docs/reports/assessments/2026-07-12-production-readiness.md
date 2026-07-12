# 2026-07-12 실사용 준비 점검

운영 대체값을 제거한 뒤 현재 머신에서 실제 서비스 준비 여부를 기록한다. `ready`는 필수 구성요소가
실행 가능하다는 뜻이며, 실제 논문 품질이나 외부 공개 production 완료를 뜻하지 않는다.

```text
코드 회귀 검증: 통과
운영 설정 검증: 통과
PostgreSQL·BGE-M3·Ollama: 실제 기동 확인
Paddle OCR: 런타임·로컬 모델·API 업로드 확인
구성요소 판정: ready
품질 판정: 실제 논문 평가 전, 미확정
```

# 1단계: 실행한 검증

```bash
PYTHONPATH=.venv/lib/python3.12/site-packages:src \
  ./scripts/with_paddle_runtime.sh python3 -m pytest -q
.venv/bin/ruff check src tests scripts
python3 -m compileall -q src tests scripts
docker compose config -q
PYTHONPATH=src ./scripts/with_paddle_runtime.sh .venv/bin/python scripts/preflight.py
curl -s http://localhost:8000/ready | python -m json.tool
```

| 검증 | 결과 |
| --- | --- |
| pytest | 78 passed, 의존성 경고 3건 |
| ruff | 통과 |
| compileall | 통과 |
| compose config | 통과 |
| `/ready` | HTTP 200, `ready`, 오류·경고 없음 |

# 2단계: 실제 모델과 서비스 실측

| 구성 | 결과 |
| --- | --- |
| PaddlePaddle | 3.3.0 CPU, `run_check` 통과 |
| PaddleOCR / PaddleX | 3.7.0 / 3.7.2 |
| 레이아웃 | `PP-DocLayout-M`, 로컬 23MB |
| 텍스트 검출 | `PP-OCRv5_mobile_det`, 로컬 4.8MB |
| 텍스트 인식 | `korean_PP-OCRv5_mobile_rec`, 로컬 14MB |
| 합성 PDF API 업로드 | HTTP 200, `full_ocr`, 1페이지 3블록 |
| 실제 CC BY 논문 smoke | HTTP 200, 1페이지 약 5분 30초, 7블록; 제목·Abstract 누락 |
| 클릭 검수 viewer | HTTP 200, 영역 좌표와 OCR 원문 반환 |
| 200 DPI CPU 처리 | 모델 초기화 포함 약 60~70초/1페이지(단일 실측) |
| PostgreSQL 16+pgvector | 컨테이너 기동, 마이그레이션, `vector` 확장 확인 |
| BGE-M3 | 로컬 캐시 4.3GB, 오프라인 CPU 추론 `(2, 1024)` 확인 |
| Ollama | `qwen2.5:7b-instruct-q4_K_M` 4.68GB, JSON 응답 확인 |

표 구조 후보인 `PP-LCNet_x1_0_table_cls`, `SLANeXt_wired`, `SLANet_plus`도 내려받았지만 현재 운영
경로에는 연결하지 않았다. PP-StructureV3의 정밀 셀 파이프라인은 추가 대형 셀 검출 모델을 요구해 CPU
MVP 기본값과 맞지 않는다. 현재는 레이아웃 모델이 찾은 표 영역 안의 OCR 토큰을 좌표로 행·열 정렬해
텍스트를 보존한다. 병합 셀·복잡한 무선 표의 정확한 구조 복원을 보장하지 않는다.

# 3단계: 확인된 운영 방어선

| 항목 | 결과 |
| --- | --- |
| 모든 PDF OCR backend | `paddle`, 정상 |
| 의미 임베딩 | `st`, 1024차원 실제 BGE-M3 확인 |
| LLM 조용한 폴백 | 차단됨 |
| simple/docling 운영 업로드 | 차단됨 |
| hash 임베딩 운영 사용 | `/ready`에서 차단됨 |
| 로컬 모델 누락 | `/ready` 오류로 공개 |

# 4단계: 숨기지 않는 품질 공백

합성 문서의 제목 오분류에 이어 실제 LayoutLMv2 첫 페이지에서도 제목·Abstract와 왼쪽 본문 일부가
누락됐다. 따라서 실제 API가 동작한 사실과 레이아웃 품질 합격은 구분한다. 아직 실제 한글·영문 과학 논문
평가셋, 클래스별 mAP, OCR
CER, 표 TEDS, 단락 F1, 검색 recall을 측정하지 않았다. 동기식 HTTP OCR도 긴 문서의 시간 초과와 동시
처리 문제를 남긴다.

# 5단계: 다음 승인 게이트

1. 저장·재가공 권리가 확인된 실제 디지털 5편과 스캔 5편을 업로드한다.
2. 레이아웃 누락·오분류, OCR 원문, 표 텍스트와 처리 시간을 기록한다.
3. 기준선이 목표에 못 미칠 때만 큰 모델, 표 셀 파이프라인 또는 Colab 파인튜닝을 선택한다.
4. 외부 공개 전 비동기 작업 API, 인증·TLS·악성 PDF 격리와 감사용 모델 버전을 구현한다.

## 완료 체크리스트

- [x] 코드·설정 회귀 테스트를 실행했다.
- [x] 개발용 대체값을 운영에서 차단했다.
- [x] Paddle 런타임과 핵심 로컬 모델을 준비했다.
- [x] BGE-M3·Ollama·PostgreSQL과 `/ready`를 확인했다.
- [x] 합성 PDF의 실제 OCR 업로드와 클릭 검수 API를 확인했다.
- [ ] 실제 PDF 10편의 OCR·검색·Excel 실사용 검증을 완료했다.
- [ ] 정량 품질과 처리량 합격선을 충족했다.
