"""STEP 6 keywords — 키워드 표기 정규화와 논문-키워드 점수 산식.

`normalize()`는 Kiwi 형태소 분석으로 표기 변형(조사·활용형 차이 등)을 흡수해
"정확 매칭률"을 확보하는 DESIGN.md §2의 근거를 구현한 것이다. 여기서 만든 정규화
키워드는 `repository.upsert_keyword`가 `keywords.keyword`(UNIQUE) 값으로 사용하며,
동일 정규화 키워드가 없을 때는 임베딩 유사도 ≥0.95인 기존 키워드에 동의어
(`keyword_aliases`)로 병합한다(그 병합 로직 자체는 `repository.py`에 있다).

`KeywordScore`/`score_keyword`는 `paper_keywords.score`(DESIGN.md §4) 산식
0.3×제목 등장 + 0.2×초록 등장 + 0.2×본문 등장 빈도(정규화) + 0.3×저자 지정
("Keywords:"/"CCS Concepts:" 블록에서 추출, `pipeline._extract_author_keywords`
참고)을 구현한다.
"""

import re
from dataclasses import dataclass
from functools import lru_cache


@lru_cache(maxsize=1)
def _kiwi():
    """Kiwi 형태소 분석기를 지연 로딩하고 프로세스당 1개만 유지한다.

    `kiwipiepy`는 무거운 optional 의존성이므로(CLAUDE.md 코드 규칙) 코어 패키지
    임포트 시점이 아니라 최초 사용 시점에 임포트를 시도한다. 설치되어 있지 않으면
    None을 반환해 이후 정규화가 공백 정리만 하는 폴백으로 동작하게 한다.
    """
    try:
        from kiwipiepy import Kiwi  # type: ignore[import-not-found]
    except ImportError:
        return None
    return Kiwi()


def normalize(kw: str) -> str:
    """키워드 표기를 정규화한다: 공백 정리+소문자화 후 가능하면 Kiwi 형태소 분석 결과(표제어)로 재조합.

    Kiwi가 없거나 분석에 실패하면 조용히 폴백 정규화(`_fallback_normalize`) 결과만
    반환한다 — 이 키워드는 검색·저장 전 구간에서 "동일 개념"을 판정하는 기준 키가
    되므로 예외로 파이프라인 전체를 막지 않고 항상 값을 반환하도록 방어한다.
    """
    text = _fallback_normalize(kw)
    kiwi = _kiwi()
    if kiwi is None or not text:
        return text

    try:
        analyses = kiwi.analyze(text)
    except Exception:
        return text
    if not analyses:
        return text

    tokens = analyses[0][0]
    forms: list[str] = []
    for token in tokens:
        value = getattr(token, "lemma", None) or getattr(token, "form", "")
        value = str(value).strip()
        if value:
            forms.append(value)
    return _fallback_normalize(" ".join(forms)) or text


def _fallback_normalize(text: str) -> str:
    """Kiwi 없이도 항상 적용되는 최소 정규화: 연속 공백 축약 + 대소문자 무시."""
    return re.sub(r"\s+", " ", text.strip().lower())


@dataclass(frozen=True)
class KeywordScore:
    """`paper_keywords.score` 산식의 가중치 컨테이너(DESIGN.md §4).

    기본값 0.3/0.2/0.2/0.3은 "제목·저자 지정 키워드가 가장 중요하고, 초록·본문
    등장은 비슷한 비중으로 본다"는 설계 결정을 반영한다. author_weight는 저자가
    "Keywords:"/"CCS Concepts:"로 직접 명시한 키워드에만 적용되는 가중치로,
    LLM이 title/abstract만 보고 독립적으로 그 키워드를 제안하지 못했더라도
    최소한의 점수를 보장해 완전히 묻히지 않게 한다.
    """

    title_weight: float = 0.3
    abstract_weight: float = 0.2
    body_weight: float = 0.2
    author_weight: float = 0.3

    def compute(
        self,
        keyword: str,
        *,
        title: str,
        abstract: str,
        body_frequency: int,
        max_body_frequency: int,
        is_author_keyword: bool = False,
    ) -> float:
        """키워드 하나의 논문 대표성 점수를 계산한다.

        제목/초록 등장과 저자 지정 여부는 있다/없다(0 또는 1)만 보고, 본문 등장은
        이 논문 내에서 가장 많이 등장한 키워드 빈도(max_body_frequency) 대비 상대
        빈도로 정규화해 단락 수가 많은 논문에 유리해지지 않게 한다.
        """
        normalized_keyword = normalize(keyword)
        title_hit = 1.0 if normalized_keyword and normalized_keyword in normalize(title) else 0.0
        abstract_hit = 1.0 if normalized_keyword and normalized_keyword in normalize(abstract) else 0.0
        body_norm = body_frequency / max_body_frequency if max_body_frequency > 0 else 0.0
        return (
            self.title_weight * title_hit
            + self.abstract_weight * abstract_hit
            + self.body_weight * body_norm
            + self.author_weight * (1.0 if is_author_keyword else 0.0)
        )


def score_keyword(
    keyword: str,
    *,
    title: str,
    abstract: str,
    body_frequency: int,
    max_body_frequency: int,
    is_author_keyword: bool = False,
) -> float:
    """`KeywordScore`의 기본 가중치로 점수를 계산하는 간단한 함수형 래퍼."""
    return KeywordScore().compute(
        keyword,
        title=title,
        abstract=abstract,
        body_frequency=body_frequency,
        max_body_frequency=max_body_frequency,
        is_author_keyword=is_author_keyword,
    )
