# 05. 검색 API와 엑셀 다운로드

자연어 질의를 대표 논문과 연관 논문으로 해석하고, 결과 엑셀을 내려받는 API를 검증한다.

```text
사용자
  └─ POST /search
       ├─ matched: 대표/연관 논문 결과 + result_id
       └─ suggest: 유사 키워드 Top-3 + session_id
            └─ POST /search/select
                 └─ matched: 대표/연관 논문 결과 + result_id
                      └─ GET /result/{result_id}/excel
```

# 1단계: 사전 조건 확인

| 항목 | 값 | 설명 |
| --- | --- | --- |
| 스택 | PostgreSQL 16 + pgvector, Ollama, 임베딩 서버 | 폐쇄망 내부에서 이미 기동되어 있어야 한다. |
| 스키마 | `db/migrations/0001_init.sql` | `papers`, `paragraphs`, `keywords`, `paper_keywords`, `paper_relations`, `search_results`가 필요하다. |
| 데이터 | 수집 파이프라인 STEP 1~8 완료 | 키워드, 단락 임베딩, 논문 연관도가 적재되어야 한다. |
| 결과 위치 | `PAPERRAG_RESULT_DIR` | 생성된 `.xlsx` 파일 캐시 디렉터리다. |

```bash
docker compose ps
```

검증:
```bash
docker compose ps   # postgres, ollama, embedding 관련 서비스가 running
```

> 주의: 유료 DB나 사내 논문 원문을 엑셀로 재출력하기 전에 이용 범위와 재배포 조건을 먼저 확인한다.

# 2단계: API 서버 기동

| 항목 | 값 | 설명 |
| --- | --- | --- |
| 앱 | `paperrag.search.api:app` | FastAPI 엔트리포인트 |
| 호스트 | `PAPERRAG_API_HOST` | 기본값 `0.0.0.0` |
| 포트 | `PAPERRAG_API_PORT` | 기본값 `8000` |

```bash
uvicorn paperrag.search.api:app --host "${PAPERRAG_API_HOST:-0.0.0.0}" --port "${PAPERRAG_API_PORT:-8000}"
```

검증:
```bash
curl -s http://localhost:8000/health
```

기대 응답:
```json
{"status":"ok"}
```

# 3단계: `/search` 호출

정확 매칭이 가능한 질의는 바로 `matched` 응답을 반환한다.

```bash
curl -s -X POST http://localhost:8000/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"온프레미스 RAG 검색 정확도 논문"}'
```

응답 예시:
```json
{
  "status": "matched",
  "matched_keyword": "RAG",
  "match_type": "exact",
  "result_id": "r-20260704-1a2b3c4d",
  "primary_paper": {
    "paper_id": 101,
    "title": "On-premises RAG Retrieval Study",
    "authors": "Kim; Lee",
    "published_year": 2025,
    "journal": "Journal of Search",
    "full_text_link": "https://example.test/paper/101",
    "keywords": ["RAG", "검색 정확도"],
    "score": 0.89,
    "reason": "대표 점수=0.890 (키워드 0.800*0.5=0.400, 단락 1.000*0.3=0.300, 제목/초록 1.000*0.1=0.100, 연도 0.900*0.1=0.090)"
  },
  "related_paper": {
    "paper_id": 205,
    "title": "Related OCR Retrieval Paper",
    "authors": "Choi",
    "published_year": 2024,
    "journal": "Related Journal",
    "full_text_link": null,
    "keywords": ["OCR", "RAG"],
    "score": 0.77,
    "reason": "겹치는 키워드: RAG"
  }
}
```

정확 매칭이 없으면 유사 키워드 후보와 `session_id`를 반환한다.

```bash
curl -s -X POST http://localhost:8000/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"문서 의미 검색 성능 비교"}'
```

응답 예시:
```json
{
  "status": "suggest",
  "session_id": "6d05f6b0-8d5d-44fa-a33f-7b4fd4fdad40",
  "candidates": [
    {"keyword_id": 1, "keyword": "RAG", "similarity": 0.91},
    {"keyword_id": 2, "keyword": "Vector Search", "similarity": 0.84},
    {"keyword_id": 3, "keyword": "Semantic Retrieval", "similarity": 0.78}
  ]
}
```

검증:
```bash
curl -s -X POST http://localhost:8000/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"온프레미스 RAG 검색 정확도 논문"}' | python -m json.tool
```

| 점수 | 공식 | 설명 |
| --- | --- | --- |
| 대표 논문 | `0.5*키워드 점수 + 0.3*단락 최고 유사도 + 0.1*제목/초록 등장 + 0.1*연도 가중치` | 키워드에 연결된 논문 중 최고 1편을 고른다. |
| 연관 논문 | `paper_relations.relation_score` | 수집 단계에서 미리 계산한 연관도 중 최고 1편을 고른다. |

# 4단계: `/search/select` 호출

`suggest` 응답에서 사용자가 고른 `keyword_id`를 세션과 함께 보낸다.

```bash
curl -s -X POST http://localhost:8000/search/select \
  -H 'Content-Type: application/json' \
  -d '{"session_id":"6d05f6b0-8d5d-44fa-a33f-7b4fd4fdad40","keyword_id":1}'
```

응답 예시:
```json
{
  "status": "matched",
  "matched_keyword": "RAG",
  "match_type": "selected",
  "result_id": "r-20260704-9f8e7d6c",
  "primary_paper": {
    "paper_id": 101,
    "title": "On-premises RAG Retrieval Study",
    "authors": "Kim; Lee",
    "published_year": 2025,
    "journal": "Journal of Search",
    "full_text_link": "https://example.test/paper/101",
    "keywords": ["RAG", "검색 정확도"],
    "score": 0.89,
    "reason": "대표 점수=0.890 (키워드 0.800*0.5=0.400, 단락 1.000*0.3=0.300, 제목/초록 1.000*0.1=0.100, 연도 0.900*0.1=0.090)"
  },
  "related_paper": null
}
```

검증:
```bash
curl -i -X POST http://localhost:8000/search/select \
  -H 'Content-Type: application/json' \
  -d '{"session_id":"expired-session","keyword_id":1}'   # HTTP/1.1 404
```

> 주의: suggest 세션 TTL은 30분이다. 만료된 `session_id`로 `/search/select`를 호출하면 404를 반환한다.

# 5단계: 엑셀 다운로드

`matched` 응답의 `result_id`로 엑셀 파일을 내려받는다.

```bash
curl -L -o paper-search-r-20260704-1a2b3c4d.xlsx \
  http://localhost:8000/result/r-20260704-1a2b3c4d/excel
```

검증:
```bash
python - <<'PY'
from openpyxl import load_workbook

wb = load_workbook("paper-search-r-20260704-1a2b3c4d.xlsx", read_only=True)
print(wb.sheetnames)
PY
```

| 시트 | 내용 |
| --- | --- |
| 검색 결과 요약 | 질의, 매칭 키워드, 매칭 방식, 대표/연관 논문 제목, 유사도, 선정 사유, 생성 일시 |
| 대표 논문 정보 | 제목, 저자, 연도, 저널, 초록 요약, 전문 링크, 키워드 |
| 대표 논문 단락 | 단락 번호, 섹션명, 원문, 정제문, 요약, 키워드 |
| 연관 논문 정보 | 대표 논문 정보 항목 + 연관 점수, 연관 사유 |
| 연관 논문 단락 | 단락 번호, 섹션명, 원문, 정제문, 요약, 키워드 |
| 표 데이터 | 구분, 표 제목, 표 내용, 표 요약 |

> 주의: `result_id`는 `search_results`와 `PAPERRAG_RESULT_DIR`의 파일 경로를 함께 캐시한다. DB 레코드나 파일 중 하나가 삭제되면 다운로드는 404로 처리한다.

# 6단계: 검증

| 항목 | 명령 | 기대 결과 |
| --- | --- | --- |
| API import | `python -c "import paperrag.search.api"` | 오류 없이 종료 |
| 테스트 | `pytest` | 검색 서비스, 엑셀, API 테스트 통과 |
| 다운로드 | `curl -I /result/{result_id}/excel` | `200 OK`, xlsx 파일명 |

```bash
python -c "import paperrag.search.api"
pytest
```

검증:
```bash
pytest tests/test_search_service.py tests/test_excel.py tests/test_search_api.py
```

## 완료 체크리스트
- [ ] `/health`가 `{"status":"ok"}`를 반환한다.
- [ ] `/search`가 `matched`와 `suggest` 응답을 모두 반환한다.
- [ ] `/search/select`가 선택 키워드로 결과를 생성한다.
- [ ] `/result/{result_id}/excel`이 6시트 엑셀을 다운로드한다.
- [ ] 세션 만료와 결과 캐시 누락이 404로 처리된다.
