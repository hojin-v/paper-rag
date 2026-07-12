# 발표자료 구성

| 파일 | 용도 |
| --- | --- |
| `PRESENTATION.md` | 화면에 표시하는 12장 발표 슬라이드 |
| `SPEAKER_NOTES_AND_QA.md` | 슬라이드별 발표자 설명과 평가자 예상 질문·답변 |
| `assets/architecture.svg` | 전체 시스템 아키텍처 |
| `assets/user-flows.svg` | 등록 사용자와 검색 사용자 흐름 |
| `assets/model-routing.svg` | 모든 PDF에 적용하는 단일 OCR 모델 흐름 |

## 사용 원칙

- 발표 화면에는 `PRESENTATION.md`만 사용한다.
- 긴 설명은 슬라이드에 추가하지 않고 `SPEAKER_NOTES_AND_QA.md`에서 발표자가 말한다.
- 평가자가 기능 범위, 모델 근거, CPU 가능성, 품질 검증을 질문하면 해당 슬라이드의 Q&A를 사용한다.
- 현재 구현과 다음 목표를 섞어 말하지 않는다. `현재와 목표` 슬라이드에서 명시적으로 구분한다.

## 렌더링

`PRESENTATION.md`는 Marp 형식이다. Marp를 지원하는 편집기에서 열면 16:9 슬라이드로 발표하거나
PDF/PPTX로 내보낼 수 있다. Marp가 없어도 일반 Markdown 문서로 내용과 이미지를 확인할 수 있다.
