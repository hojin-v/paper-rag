# 11. 라이선스 확인 논문 수집

공개 여부만 믿고 원문을 저장하지 않고, 재사용 가능한 라이선스가 확인된 PDF만 테스트 입력으로 수집한다.

```text
OpenAlex Works API
├─ 주제 검색 또는 고정 work ID 조회
├─ OA + PDF URL + CC 라이선스 필터
└─ 원 출판처 PDF 다운로드
    ├─ PDF 시그니처·최대 크기 검증
    ├─ SHA-256·출처·라이선스 manifest
    ├─ data/inbox/collected/   전체 논문
    └─ data/inbox/smoke/       첫 1페이지 빠른 OCR 점검본
```

# 1단계: 수집원 선정 결과

| 수집원 | 인증 | 원문·권리 정보 | 현재 적용 |
| --- | --- | --- | --- |
| OpenAlex | 무료 API key, 소량 익명 요청은 현재 동작하나 비보장 | OA 위치의 PDF URL과 개별 license 제공 | 영문 자동 수집 기본값 |
| Europe PMC | 무인증 | 생명과학 OA subset 약 700만 편, 논문별 라이선스 확인 필요 | 생명과학 확장 후보 |
| arXiv | 무인증 | PDF는 풍부하지만 기본 arXiv 라이선스는 제3자 재사용을 제한 | CC 여부를 별도 확인하지 않는 자동 수집 금지 |
| KCI OAI-PMH | 무인증 | 한글 메타데이터·원문공개 여부 제공, CCL·직접 PDF 권리는 별개 | 한글 원문 규모 실사용 전 조사 |
| ScienceON | 무료 신청형 key | 국내외 논문 검색 메타데이터 | key 발급 후 보조 발견 경로 |

초기 자동 평가셋은 영어 논문으로 구성한다. 이는 영어 모델이 무조건 우수해서가 아니라, 현재 API에서
개별 CC 라이선스와 직접 PDF를 함께 검증할 수 있기 때문이다. 한글 논문은 KCI의 `원문공개=Y`만으로
OCR·요약·단락 재출력 권한을 추정하지 않고 OAK/학술지 CCL을 추가 대조한 뒤 편입한다.

> OpenAlex도 PDF에 새로운 권리를 부여하지 않는다. 이 수집기는 `cc-by`, `cc-by-sa`, `cc0`만 허용하며
> `other-oa`, 라이선스 미상, NC, ND 논문은 코드에서 거절한다.

# 2단계: OpenAlex 무료 키 설정

[OpenAlex 설정 화면](https://openalex.org/settings/api)에서 무료 키를 발급해 `.env`에 넣는다.

```bash
PAPERRAG_OPENALEX_API_KEY=발급받은_키
PAPERRAG_OPENALEX_CONTACT_EMAIL=담당자_이메일
```

무료 key는 일일 무료 사용량이 있으며 대량 수집 전 사용량 헤더와 정책을 확인한다. 현재 익명 smoke
요청도 성공하지만 공식 운영 조건으로 의존하지 않는다.

검증:

```bash
PYTHONPATH=src .venv/bin/python -m paperrag.collect \
  --query "document layout analysis OCR" --limit 3 --dry-run
```

dry-run은 파일을 저장하지 않고 source ID, 라이선스, 제목과 PDF URL만 출력한다.

# 3단계: 논문 다운로드

주제 검색:

```bash
PYTHONPATH=src .venv/bin/python -m paperrag.collect \
  --query "document layout analysis OCR" --limit 3
```

동일한 테스트셋 재현:

```bash
PYTHONPATH=src .venv/bin/python -m paperrag.collect \
  --work-id W3176851559 \
  --work-id W4226020328 \
  --work-id W4402670290
```

| Work ID | 용도 | 라이선스 | 페이지 |
| --- | --- | --- | ---: |
| `W3176851559` | LayoutLMv2, 복합 문서 레이아웃 | CC BY | 13 |
| `W4226020328` | LiLT, 언어 독립 레이아웃 | CC BY | 11 |
| `W4402670290` | RAG 검색·생성 평가 | CC BY | 19 |

동일 source ID의 파일과 SHA-256이 일치하면 다시 다운로드하지 않는다. 손상됐거나 checksum이 바뀐
파일은 다시 받고 manifest를 갱신한다. HTML 오류 페이지나 최대 크기 초과 응답은 `.pdf`로 남기지 않는다.

검증:

```bash
find data/inbox/collected -maxdepth 1 -name "*.pdf" -type f
head -n 3 data/inbox/collected/collection-manifest.jsonl
```

# 4단계: CPU용 빠른 OCR 점검본

전체 논문은 11~19페이지라 CPU 전 페이지 OCR에 시간이 걸린다. 기능 경로만 먼저 확인할 때는 출처가
연결된 첫 1페이지 파생본을 만든다.

```bash
PYTHONPATH=src .venv/bin/python -m paperrag.collect.smoke
```

`data/inbox/smoke/collection-manifest.jsonl`에는 원본 checksum, 파생 페이지 범위와
`pipeline-smoke-test-only` 용도가 기록된다. smoke 결과는 실제 논문 전체의 OCR 품질 지표로 사용하지 않는다.

실제 OCR dry-run:

```bash
PYTHONPATH=.venv/lib/python3.12/site-packages:src \
  ./scripts/with_paddle_runtime.sh python3 -m paperrag.ingest \
  data/inbox/smoke --backend paddle --skip-llm --dry-run
```

전체 적재는 `data/inbox/collected`를 입력하고 `--skip-llm --dry-run`을 제거한다.

# 5단계: 보존하는 출처 정보

| 필드 | 의미 |
| --- | --- |
| `source_provider`, `source_id` | OpenAlex와 work ID |
| `title`, `authors`, `publication_year`, `doi` | API 메타데이터 |
| `landing_page_url`, `pdf_url`, `source_name` | 출처와 원 파일 위치 |
| `license` | 수집 당시 허용 라이선스 |
| `sha256`, `byte_size`, `retrieved_at` | 파일 무결성과 수집 시점 |
| `derived_from_sha256`, `page_range`, `purpose` | smoke 파생본 추적 |

현재 이 정보는 JSONL manifest에 보존되고 `papers` DB 레코드에는 자동 복사되지 않는다. 외부 공개
production 전에 provenance 테이블 또는 papers 확장 컬럼과 처리 run을 연결해야 한다.

실측 기록은
[`2026-07-12-paper-collection-smoke.md`](../reports/assessments/2026-07-12-paper-collection-smoke.md)에
있다. LayoutLMv2 첫 페이지는 약 5분 30초에 7개 영역을 반환했지만 제목·Abstract가 누락됐으므로,
smoke 성공을 실제 논문 품질 합격으로 해석하지 않는다.

## 완료 체크리스트

- [x] OpenAlex·KCI OAI-PMH·Europe PMC 실제 API 응답을 확인했다.
- [x] 허용 라이선스 목록과 PDF 시그니처·용량을 코드에서 검증한다.
- [x] 고정 3편을 포함한 CC BY/CC BY-SA 영어 논문 66편·1,175페이지와 SHA-256 manifest를 준비했다.
- [x] CPU 빠른 확인용 1페이지 smoke PDF 66편을 준비했다.
- [ ] OpenAlex 무료 API key와 담당자 이메일을 운영 환경에 설정했다.
- [ ] 한글 논문의 CCL·재가공 권리를 대조한 수용 테스트셋을 마련했다.
- [ ] provenance manifest를 DB processing run과 연결했다.
