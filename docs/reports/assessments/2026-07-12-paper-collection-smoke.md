# 2026-07-12 논문 API 수집·OCR smoke 점검

무료 논문 API의 실제 응답, 권리 필터와 수집 PDF의 현재 OCR 연결 결과를 기록한다.

```text
OpenAlex 검색·고정 ID 조회: 성공
KCI OAI-PMH 메타데이터: 성공
Europe PMC OA 검색: 성공
CC BY PDF 3편 다운로드·checksum: 성공
실제 논문 1페이지 Paddle OCR·viewer: 성공
레이아웃 품질: 제목·Abstract 누락으로 미합격
```

# 1단계: API 실제 응답

| API | 요청 결과 | 판정 |
| --- | --- | --- |
| OpenAlex Works | HTTP 200, OA PDF와 `best_oa_location.license` 반환 | 영문 자동 수집 기본 |
| KCI OAI-PMH | HTTP 200, `oai_kci` 논문 메타데이터 반환 | 한글 메타·공개 규모 조사 |
| Europe PMC REST | HTTP 200, OA RAG 검색 결과 반환 | 생명과학 확장 후보 |
| arXiv | 검색·PDF는 가능 | 기본 라이선스 재사용 제한 때문에 자동 원문 수집 제외 |

# 2단계: 고정 테스트 논문

| Work ID | 파일 크기 | SHA-256 | 라이선스 |
| --- | ---: | --- | --- |
| `W3176851559` | 995,010 | `f445e0aaf1ecf32e1efb67868e632426774192726a5c051e00aa743404ba2251` | CC BY |
| `W4226020328` | 1,879,437 | `36cdf026f7a0404711f12ffdffa977194a9651fd552c46509f0c5a41c74396cf` | CC BY |
| `W4402670290` | 771,621 | `b8f47826d8a20a02c81644ff6b1d43bea5c0e5b7187da590edb015cab28f4640` | CC BY |

전체 PDF는 각각 13, 11, 19페이지다. 원본과 첫 1페이지 smoke 파생본 모두 별도 JSONL manifest로
출처·라이선스·checksum을 추적한다.

# 3단계: 100건 후보 일괄 수집

`document layout analysis OCR` 검색으로 OpenAlex 상위 100건을 요청했다. HTTPS PDF URL·필수
메타데이터 검증을 통과한 후보는 92건이었다.

| 결과 | 건수 |
| --- | ---: |
| 신규 다운로드 | 63 |
| 기존 checksum 일치로 생략 | 2 |
| 출판처 403·404·HTML 응답 등 실패 | 27 |
| 최종 보유 PDF | 66 |
| 최종 페이지 | 1,175 |
| 최종 용량 | 216,487,681 bytes |
| CC BY / CC BY-SA | 61 / 5 |
| PDF 시그니처·checksum·열기 오류 | 0 |
| 첫 1페이지 smoke set | 66편, 15,970,480 bytes, 오류 0 |

실행 후 OpenAlex 일일 사용량은 `$0.0023`, 잔여량은 `$0.9977`이었다. OpenAlex 비용보다 원 출판처의
접근 제한과 이후 CPU OCR 처리량이 실제 병목이다. 실패 응답은 우회하지 않았고 manifest에도 넣지 않았다.

# 4단계: 실제 논문 OCR

`W3176851559` LayoutLMv2의 첫 페이지를 200 DPI Paddle 전체 OCR API에 업로드했다.

| 항목 | 결과 |
| --- | --- |
| API | `POST /documents`, HTTP 200 |
| document ID | `228def9c17994639a35cc8e6b8a93a39` |
| backend | `paddle`, `full_ocr` |
| 처리 시간 | 약 5분 30초, CPU 약 2코어 지속 사용 |
| 검출 영역 | 7개 |
| viewer | HTTP 200, 7개 block ID 포함 |
| 성공 영역 | 저자, Introduction 제목, 양단 본문 일부, 페이지 footer |
| 누락 영역 | 논문 제목, Abstract, 왼쪽 열 본문 일부 |

합성 1페이지의 약 60~70초보다 실제 2단 논문이 훨씬 느렸다. 현재 동기 HTTP 방식으로 11~19페이지
전체를 처리하면 사용자 대기와 timeout 위험이 크다. 이 결과는 실제 경로 성공 근거이지 mAP·CER·단락
F1 합격 근거가 아니다.

# 5단계: 후속 판단

1. 나머지 2개 smoke 첫 페이지를 검수해 같은 누락이 반복되는지 확인한다.
2. 10편 수용 테스트 전 업로드를 비동기 작업 API로 전환한다.
3. 제목·Abstract 누락률이 목표를 넘으면 PP-DocLayout-L 비교 후 Colab 파인튜닝을 결정한다.
4. 한글은 KCI 공개 여부와 별도로 개별 CCL을 대조한 5편을 수동 확보한다.

## 완료 체크리스트

- [x] 실제 API 세 곳의 응답을 확인했다.
- [x] 허용 라이선스 PDF 66편·1,175페이지를 checksum과 함께 저장했다.
- [x] 전체 66편의 첫 1페이지 smoke set을 만들고 실제 한 편으로 클릭형 OCR 경로를 확인했다.
- [x] 처리 시간과 레이아웃 누락을 숨기지 않고 기록했다.
- [ ] 실제 논문 10편 정량 평가를 완료했다.
