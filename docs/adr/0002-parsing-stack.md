# ADR-0002: 문서 파싱 이중 트랙 — Docling(디지털) + PP-StructureV3(스캔·파인튜닝)

- 상태: 승인 (2026-07-04)

## 배경

레이아웃 분석 모델 선정 기준에 "도메인 파인튜닝 가능성"과 "온프레미스 상용 라이선스 안전성"을 추가해 재검증했다.

## 결정

- 디지털 PDF(텍스트 레이어 ≥80%): **Docling** (MIT) — OCR 불필요, 읽기 순서·TableFormer 표 인식 품질 우수
- 스캔 PDF + 파인튜닝 대상 트랙: **PaddleOCR PP-StructureV3** (Apache 2.0) — PP-DocLayout·SLANeXt·PP-OCRv5 한국어가 모두 PaddleX로 파인튜닝 공식 지원
- 최종 확정은 Phase 0 자체 평가셋 50편 실측(mAP/CER/TEDS/E2E F1)으로 Docling·PP-StructureV3·MinerU 3자 벤치마크 후 수치로 판단

## 배제

| 후보 | 배제 사유 |
| --- | --- |
| LayoutLMv3/DiT | 가중치 CC BY-NC (비상업) |
| Surya/Marker | GPL + 가중치 상용 조건부 |
| MinerU | AGPL-3.0 — 벤치마크 기준(baseline)으로만 사용 |
| DocLayout-YOLO | AGPL-3.0 — 성능 백업 후보로 보류 |

## 영향

- 파이프라인 STEP 2(layout)는 백엔드 교체 가능한 어댑터 인터페이스로 구현한다 (Docling/PP-Structure/스텁)
- 파인튜닝 계획(DESIGN.md §6)의 어노테이션 클래스는 필터링 규칙과 동일한 12클래스로 고정
