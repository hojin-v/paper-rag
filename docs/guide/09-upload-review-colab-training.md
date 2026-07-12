# 09. PDF 자동 분석 품질 모니터와 관리자 모델 개선

운영자는 레이아웃·OCR 자동화 과정과 품질 판정 근거를 읽기 전용으로 모니터링한다. 일반 검색 사용자는
개입하지 않으며, 품질 기준을 통과하지 못한 문서만 관리자가 분석해 학습데이터로 사용한다.

```text
Streamlit PDF 업로드
  → 모든 페이지 이미지 변환
  → PP-DocLayout-M 레이아웃 검출(텍스트는 아직 없음)
  → 텍스트 검출 좌표로 잘린 박스 확장·누락 본문 추가·중복 제거
  → 검출 박스 crop OCR
       └─ 표: PP-LCNet 분류 → SLANeXt_wired / SLANet_plus
  → OCR 영역 인식률·제목·표 구조화 자동 판정
  ├─ 합격: DB·pgvector 적재 후보
  └─ 실패: 관리자 예외 대기열
       → 오류 분석·정답 교정
       → Colab 학습데이터 ZIP
       → Colab GPU 학습
       → 모델 ZIP 내려받기
       → 온프레미스 models/에 반입
```

# 1단계: 자동 분석 품질 모니터 실행

API와 UI를 각각 실행한다.

```bash
uvicorn paperrag.search.api:app --host 0.0.0.0 --port 8000
streamlit run src/paperrag/ui/app.py
```

UI의 `PDF 자동 분석 모니터` 탭에서 PDF를 선택하고 `업로드 후 자동 구조화 실행`을 누른다. 업로드 한 번으로
레이아웃 검출, 영역 OCR, 품질 판정까지 연속 실행되며 영역별 사람 승인을 요구하지 않는다.

- 디지털 PDF를 포함한 모든 입력은 이미지 기반 레이아웃·OCR 경로를 사용한다.
- PDF에 포함된 텍스트 레이어는 본문 추출에 사용하지 않는다.
- PaddleOCR가 없으면 다른 파서로 자동 대체하지 않고 분석 실패를 명시한다.
- 1단계에서는 자동 레이아웃 유형과 좌표를 읽기 전용으로 확인한다. OCR 원문은 비어 있어야 한다.
- `다음 자동화 단계`를 누르면 사람의 승인 없이 일반 영역과 표 영역 OCR 및 품질 판정을 실행한다.
- 초기 레이아웃 텍스트 커버리지와 자동 보정 수가 품질 모니터에 기록된다.
- OCR 영역 인식률, 제목 인식·인용 메타데이터 일관성, 표 구조화 품질이 모두 기준을 통과해야 적재 가능 상태가 된다.
- 아래 편집 기능은 자동 품질 실패 문서의 모델 개선을 위한 관리자 도구이며 정상 사용자 흐름이 아니다.
- 기존 영역은 `선택 박스 이동·크기 조절`을 켠 뒤 박스 내부를 드래그해 이동하고, 네 모서리 점으로
  크기를 바꾼 다음 `검수 결과 저장`을 누른다.
- 잘못 검출된 영역은 선택 후 `선택 영역 삭제`를 누른다. 확인 후 화면과 OCR 입력에서 즉시 제거되며,
  삭제한 영역 ID·유형·페이지·좌표는 문서 경고 이력에 남는다.
- `겹친 자동 박스 정리`는 IoU 0.85 이상의 중복과 여러 하위 영역을 감싼 오검출 컨테이너를 정리한다.
  사용자가 직접 그린 박스는 자동 삭제하지 않는다.
- 2단계 OCR은 화면에 표시된 확정 좌표와 같은 crop만 입력으로 사용한다.
- OCR 결과에서 레이아웃 문제를 발견하면 `레이아웃부터 다시 수정`으로 돌아가며 기존 OCR을 폐기한다.
- OCR 단계에서는 텍스트만 교정할 수 있다. 좌표·유형 변경은 API에서도 거부한다.

# 2단계: 자동 품질 판정과 DB·Vector DB 적재

자동 품질 기준을 통과하면 `자동 구조화 결과를 DB·Vector DB에 적재`가 활성화된다. 실패 문서는 자동
적재되지 않고 관리자 예외 대기열에 남는다. Ollama, 임베딩 서버, PostgreSQL이 실행 중이어야 한다.

검수 전 원본과 페이지 이미지는 `PAPERRAG_REVIEW_DIR` 아래에 보존된다. API 요청은 다음과 같다.

| API | 용도 |
| --- | --- |
| `POST /documents` | PDF 바이트 업로드와 레이아웃 분석 |
| `GET /documents/{id}/viewer?editable=false` | 운영자용 읽기 전용 자동 처리 품질 모니터 |
| `GET /documents/{id}/viewer?editable=true` | 관리자 교정 화면 |
| `POST /documents/{id}/blocks` | 누락된 레이아웃 영역 좌표 추가 |
| `DELETE /documents/{id}/blocks/{block_id}` | 잘못된 영역 삭제와 이력 기록 |
| `PUT /documents/{id}/blocks/{block_id}` | 현재 단계에서 허용된 유형·좌표·OCR 교정 저장 |
| `POST /documents/{id}/deduplicate-layout` | 자동 검출 중복 박스 정리, 수동 박스 보존 |
| `POST /documents/{id}/run-ocr` | 승인 레이아웃을 crop해 일반/표 OCR 실행 |
| `POST /documents/{id}/auto-ocr` | 사람 승인 없이 OCR·자동 품질 판정 실행 |
| `POST /documents/{id}/reevaluate-quality` | 변경된 품질 기준으로 기존 OCR 결과 재판정 |
| `POST /documents/{id}/return-to-layout` | OCR 결과 폐기 후 레이아웃 검수로 복귀 |
| `POST /documents/{id}/confirm-ocr` | OCR 검수 완료 후 적재 가능 상태 전환 |
| `POST /documents/{id}/ingest` | DB·pgvector 적재 |
| `GET /training/export` | 승인·교정 영역을 학습 ZIP으로 생성 |

# 3단계: Colab 학습데이터 준비

UI에서 `검수 완료 학습데이터 준비`와 `Colab용 학습데이터 ZIP 다운로드`를 차례로 누른다.

ZIP에는 다음 자료가 포함된다.

- `layout/images/`: 검수한 PDF 페이지 이미지
- `layout/annotations.jsonl`: 12개 레이아웃 클래스와 bounding box
- `ocr/images/`: 영역별 OCR crop
- `ocr/labels.jsonl`: 사람이 승인·교정한 OCR 정답
- `manifest.json`: 페이지·crop 개수와 형식 버전

권장 데이터 최소량은 레이아웃 300페이지, OCR 3,000 crop이다. 초기 기능 확인은 이보다 적은 데이터로도
가능하지만 운영 모델 판단에는 사용하지 않는다. 제목·표·수식처럼 적은 클래스가 전체의 5% 미만이면
해당 페이지를 우선 검수해 보강한다. 학습/검증 분리는 페이지가 아니라 논문 단위로 자동 수행된다.

미검수 영역은 기본적으로 학습데이터에서 제외된다. 검수되지 않은 데이터까지 시험용으로 받으려면
`GET /training/export?include_unreviewed=true`를 사용한다.

# 4단계: Colab에서 버튼으로 학습

[paperrag_training_colab.ipynb](../../notebooks/paperrag_training_colab.ipynb)을 Google Drive에 올리고
Google Colab으로 연다.

1. 메뉴에서 `런타임 → 런타임 유형 변경 → T4 GPU`를 선택한다.
2. `런타임 → 모두 실행`을 한 번 누른다.
3. 파일 선택 창에서 `paperrag-training-data.zip`을 선택한다.
4. 데이터 검사 결과가 정상인지 확인한다.
5. `레이아웃 학습`, `OCR 학습`, 또는 `둘 다 순서대로 학습` 버튼을 누른다.
6. 완료 후 `모델 ZIP 다운로드` 버튼을 누른다.

학습 전후 비교 기준은 레이아웃 mAP@0.5, 읽기 순서 일치율, OCR CER, 표 TEDS, 단락 추출 F1이다.
같은 검증 논문으로 이전 모델과 비교하고 좋아진 모델만 반입한다. 권장 합격선은 레이아웃 mAP 0.85,
읽기 순서 0.95, OCR CER 3% 이하, 표 TEDS 0.85, 단락 F1 0.90이다.

노트북은 PaddleOCR 공식 저장소와 GPU 패키지를 Colab 안에서 설치한다. 로컬/온프레미스 머신에는 추론용
CPU 패키지와 사전학습 모델만 준비하며 학습은 수행하지 않는다. 학습은 수 시간이 걸릴 수 있으며
브라우저를 닫지 않는다.

# 5단계: 학습 모델 반입

Colab에서 받은 ZIP을 내부망 반입 절차에 따라 전달하고 다음처럼 배치한다.

```text
models/
├── trained/layout/  # Colab PP-DocLayout inference model
├── trained/ocr/     # Colab PP-OCR recognition inference model
└── paddlex/official_models/PP-OCRv5_mobile_det/  # 기존 검출 모델
```

`.env`에 경로를 지정하고 API를 재기동한다.

```bash
PAPERRAG_PADDLE_LAYOUT_MODEL_DIR=./models/trained/layout
PAPERRAG_PADDLE_TEXT_DETECTION_MODEL_DIR=./models/paddlex/official_models/PP-OCRv5_mobile_det
PAPERRAG_PADDLE_OCR_MODEL_DIR=./models/trained/ocr
```

PDF를 다시 올려 PP-StructureV3 모델명, 영역 좌표, OCR 결과를 확인한다.

## 완료 체크리스트

- [ ] 품질 모니터에서 자동 레이아웃 영역을 읽기 전용으로 클릭할 수 있다.
- [ ] 사람 승인 없이 영역별 OCR과 자동 품질 판정이 실행된다.
- [ ] 품질 합격 문서만 적재 가능하고 실패 문서는 예외 대기열에 남는다.
- [ ] 레이아웃 누락 영역을 페이지에서 드래그해 추가할 수 있다.
- [ ] 기존 박스를 드래그 이동·크기 조절하고 자동 중복을 정리할 수 있다.
- [ ] 레이아웃 단계에서는 OCR 결과가 비어 있고 확정 박스만 OCR 입력이 된다.
- [ ] 표 영역에 `paddle-table-structure` 엔진이 기록된다.
- [ ] 영역별 OCR 원문과 검수·교정문을 비교할 수 있다.
- [ ] 승인된 데이터만 Colab 학습 ZIP에 포함된다.
- [ ] Colab에서 데이터 검사와 학습 버튼이 표시된다.
- [ ] 모델 ZIP을 내부망에 반입하고 Paddle 모델 경로를 설정했다.
- [ ] 학습 모델로 디지털·스캔 PDF 모두 재분석했다.
