# 14. 평가셋 정답표(ground truth) 라벨링 가이드

`docs/design/DESIGN.md` §6의 합격선(레이아웃 mAP@0.5 ≥ 0.85, OCR CER ≤ 3%, 표 TEDS ≥ 0.85)을
실측하려면 사람이 교정한 정답이 필요하다. `docs/guide/10-production-readiness.md`가 "실제 과학
논문 평가셋 없음"으로 지적한 항목이 이 작업이다. `docs/guide/09-upload-review-colab-training.md`가
설명하는 검수 화면·API를 그대로 쓰되, 목적이 "모델 재학습용 데이터 생성"이 아니라 "지금 모델의
정확도를 재는 정답 만들기"라는 점이 다르다 — 그래서 무엇을 어떻게 고쳐야 정답으로 인정되는지를
이 문서에서 별도로 설명한다.

# 1단계: 라벨링 대상 10편

`collect` 모듈로 실제 다운로드한 논문 중 레이아웃 특성(단/표/수식/그림 비중)이 겹치지 않도록
10편을 골라 이미 업로드했다(`layout_review` phase, `backend=paddle`, 사람 교정 전 자동 검출
상태). `GET /documents/{id}/viewer?editable=true`로 바로 열면 된다(로컬이면
`http://localhost:8000/documents/{id}/viewer?editable=true`).

| document_id | 파일명 | 페이지 | 선정 이유 |
| --- | --- | --- | --- |
| `54ff233fef924b85804430fa34f1e856` | W2140565904 (Layout Analysis for Scanned PDF...) | 10 | 논문 자체는 디지털이나 "스캔 PDF 레이아웃" 주제 — 단일 컬럼 위주 |
| `697b623244b0453b9da4112fdbc2bba9` | W1971303936 (Biomedical figure segmentation...) | 16 | 그림·캡션 비중 높음 |
| `9ca0c0b1058c4032ba78a033abb62f7a` | W3023097746 (Mathematical Expression Detection...) | 22 | 수식(formula) 블록 다수, 2단 조판 |
| `691d37d52cc245dc8ef3cfdc51022598` | W4281677441 (historical newspapers layout...) | 25 | 복잡한 다단 레이아웃 |
| `b9785bdf3c714b30b9fcb05359eeda96` | W4392926970 (Datasets/annotations for layout analysis) | 23 | 레이아웃 주석 데이터셋 논문 — 표·그림 혼재 |
| `d41929a6189c4b498e059ec3c043a6ef` | lilt-full-section-split-final | 11 | 섹션 분리 테스트용 기존 픽스처 |
| `efceadeb91da43dba3e6fd7536491d1a` | W4387925380 (TC-OCR: TableCraft, 표 구조) | 8 | **표 비중 높음** — TEDS 정답용 |
| `f1dad1a800fa4b309062a14ad0bdf4f2` | W2891117443 (Chargrid: 2D Documents) | 11 | 폼(form)형 문서, 2D 그리드 다이어그램 |
| `0a211a5adff744cd888693cb721ce90d` | W2995225687 (MIMIC-CXR, 의료 데이터셋) | 8 | OCR/레이아웃과 무관한 도메인 — 주제 다양성 |
| `e28e6ef79333415f939d71c9a3252852` | W3034864438 (Form-like Documents) | 10 | 폼형 문서, 표 다수 |

전체 문서 목록(`GET /documents`)에는 이 10편 외에도 과거 자동화 테스트용 소형 픽스처(`smoke-1p`,
`consistency-*`, `layout-accuracy` 등 21건)가 섞여 있다 — 위 10개 `document_id`만 라벨링 대상이다.

> **알려진 공백 (교정 불가, 사용자 확인 필요)**: `collect` 모듈이 OpenAlex로 지금까지 모은 66편이
> 전부 영문·born-digital 논문이라, DESIGN.md §6이 요구하는 "디지털 5편+스캔 5편" · "한글 5편+영문
> 5편" 기준 중 **스캔 PDF와 한국어 논문은 표본에 전혀 없다**. 스캔 이미지를 흉내 내려고 디지털
> PDF를 인위적으로 노이즈·회전 처리하는 방법도 있지만 실제 스캔 품질(잉크 번짐, 접힘, 저해상도
> 인쇄)을 대체하지 못해 권장하지 않는다. 필요하면 별도로 다뤄야 한다 — 이 10편 라벨링은 우선
> "영문 디지털 논문" 범위 안에서의 정확도 측정으로 진행한다.

# 2단계: 무엇을 고치면 정답이 되는가

`ReviewBlock`은 자동 검출값과 사람 교정값을 별도 필드에 나눠 보존한다(`review/models.py`).
사람이 고치기 전까지는 두 값이 같고, 교정하면 자동 검출값(`detected_*`)은 그대로 남고 현재값만
바뀐다 — 나중에 평가 스크립트가 이 둘을 비교해 mAP·CER을 계산한다.

| 필드 | 자동 검출(모델의 "예측") | 사람 교정(정답) | 대응 지표 |
| --- | --- | --- | --- |
| 영역 좌표 | `detected_bbox` | `bbox` | 레이아웃 mAP@0.5 (IoU) |
| 블록 유형 | `detected_block_type` | `block_type` | 레이아웃 mAP@0.5 (클래스) |
| OCR 텍스트 | `ocr_text` | `corrected_text`(비었으면 `ocr_text`가 곧 정답) | OCR CER |
| 표 내용 | `ocr_text`(파이프 구분 텍스트) | `corrected_text` | 표 TEDS |

**작업 순서** (문서 하나 기준):

1. `editable=true` 뷰어를 열고 **레이아웃 단계**부터 시작한다.
   - 페이지마다 자동 검출 박스가 실제 제목/저자/본문/표/그림/수식/참고문헌 경계와 정확히
     일치하는지 확인한다. 어긋나면 박스를 드래그·리사이즈해 고치고, 유형이 틀렸으면 유형을
     바꾼다(`PUT /documents/{id}/blocks/{block_id}`).
   - 레이아웃 모델이 통째로 놓친 영역(표·수식·캡션 등)은 `선택 영역 추가`로 직접 박스를 그려
     넣는다. 이 페이지에 실제로 존재하는 모든 의미 있는 영역이 하나도 빠짐없이 박스로
     있어야 mAP의 재현율(recall)을 정확히 잴 수 있다.
   - 레이아웃 모델이 오검출한 영역(실제로는 없는 영역)은 삭제하거나 `review_status=rejected`로
     표시한다 — 삭제하면 mAP 계산에서 아예 빠지고, rejected는 "오검출로 확인됨"이라는 이력이
     남는다. 평가 목적이면 **삭제**를 권장한다(이력 없이 정답 집합에서 확실히 제외).
   - 다 됐으면 모든 블록을 승인·교정·제외 상태로 만들고(`unreviewed` 없음) `영역별 OCR 실행`
     (`POST /documents/{id}/run-ocr`)을 눌러 OCR 단계로 넘어간다. **주의**: 이 단계를 넘어가면
     좌표·유형은 잠긴다 — 레이아웃 정답은 이 단계에서 확정해야 한다.
2. **OCR 단계**로 넘어가면 각 영역의 인식 결과(`ocr_text`)를 실제 텍스트와 비교해 다르면
   `교정 텍스트`에 정확한 원문을 입력한다. 띄어쓰기·줄바꿈·특수문자까지 원문과 동일하게 맞춰야
   CER이 정확히 나온다(오탈자 교정이 아니라 "원문 그대로 받아쓰기"가 목표).
3. **표 영역**은 `ocr_text`에 이미 `셀 | 셀\n셀 | 셀` 형식(파이프로 열 구분, 줄바꿈으로 행 구분)의
   구조화 텍스트가 들어 있다. 이 형식이 실제 표의 행·열 구조와 어긋나면(셀이 합쳐지거나 누락된
   경우) `교정 텍스트`에 같은 파이프 형식으로 정답 표를 다시 쓴다 — 병합 셀은 값을 그대로
   반복해 채운다(예: 2행에 걸친 헤더는 두 행 모두 같은 값). 이 파이프 텍스트가 TEDS 계산의
   정답 표 구조로 쓰인다.
4. 텍스트까지 다 고치면 모든 블록을 승인·교정·제외 상태로 만들고 `OCR 검수 완료`
   (`POST /documents/{id}/confirm-ocr`)를 누른다. **이 문서는 적재(`/ingest`)하지 않아도 된다** —
   평가에는 `ready_to_ingest` 상태(정답 확정)까지만 필요하고, DB 적재는 검색 기능 검증용이라
   평가와 무관하다.

# 3단계: 진행 상황 확인

```bash
curl -s http://localhost:8000/documents | python3 -c "
import json, sys
docs = json.load(sys.stdin)
targets = {'54ff233fef924b85804430fa34f1e856','697b623244b0453b9da4112fdbc2bba9',
           '9ca0c0b1058c4032ba78a033abb62f7a','691d37d52cc245dc8ef3cfdc51022598',
           'b9785bdf3c714b30b9fcb05359eeda96','d41929a6189c4b498e059ec3c043a6ef',
           'efceadeb91da43dba3e6fd7536491d1a','f1dad1a800fa4b309062a14ad0bdf4f2',
           '0a211a5adff744cd888693cb721ce90d','e28e6ef79333415f939d71c9a3252852'}
for d in docs:
    if d['document_id'] in targets:
        print(d['document_id'][:8], d['phase'], d['filename'])
"
```

10편 전부 `ready_to_ingest`가 되면 라벨링이 끝난 것이다.

## 완료 체크리스트

- [ ] 10편 모두 레이아웃 누락·오검출을 정리했다(추가/삭제 포함).
- [ ] 10편 모두 OCR 텍스트를 원문과 동일하게 교정했다.
- [ ] 표가 있는 문서(TC-OCR, Chargrid, form-like documents 등)는 파이프 형식 정답 표를 확인했다.
- [ ] 10편 모두 `ready_to_ingest` 상태다(`confirm-ocr` 완료).
- [ ] 스캔 PDF·한국어 논문 표본 공백을 어떻게 할지(범위 밖으로 둘지, 별도 수집할지) 결정했다.

라벨링이 끝나면 `detected_*` vs 교정값을 비교해 실제 mAP/CER/TEDS 수치를 계산하는 스크립트가
필요하다 — 현재는 `scripts/export_ocr_evaluation.py`가 비교 데이터를 엑셀로 나열만 하고 수치를
계산하지는 않으므로, 이 부분은 라벨링 완료 후 별도로 준비한다.
