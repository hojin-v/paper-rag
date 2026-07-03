import re
from dataclasses import dataclass
from functools import lru_cache


@lru_cache(maxsize=1)
def _kiwi():
    try:
        from kiwipiepy import Kiwi  # type: ignore[import-not-found]
    except ImportError:
        return None
    return Kiwi()


def normalize(kw: str) -> str:
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
    return re.sub(r"\s+", " ", text.strip().lower())


@dataclass(frozen=True)
class KeywordScore:
    title_weight: float = 0.4
    abstract_weight: float = 0.3
    body_weight: float = 0.3

    def compute(
        self,
        keyword: str,
        *,
        title: str,
        abstract: str,
        body_frequency: int,
        max_body_frequency: int,
    ) -> float:
        normalized_keyword = normalize(keyword)
        title_hit = 1.0 if normalized_keyword and normalized_keyword in normalize(title) else 0.0
        abstract_hit = 1.0 if normalized_keyword and normalized_keyword in normalize(abstract) else 0.0
        body_norm = body_frequency / max_body_frequency if max_body_frequency > 0 else 0.0
        return (
            self.title_weight * title_hit
            + self.abstract_weight * abstract_hit
            + self.body_weight * body_norm
        )


def score_keyword(
    keyword: str,
    *,
    title: str,
    abstract: str,
    body_frequency: int,
    max_body_frequency: int,
) -> float:
    return KeywordScore().compute(
        keyword,
        title=title,
        abstract=abstract,
        body_frequency=body_frequency,
        max_body_frequency=max_body_frequency,
    )
