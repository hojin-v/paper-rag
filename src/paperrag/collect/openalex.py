"""
OpenAlex API 클라이언트 (CC 라이선스 논문 검색).

OpenAlex Works API에서 공개(OA) PDF가 있고 재사용 가능한 라이선스(cc-by/cc-by-sa/cc0 등,
`Settings.paper_collection_allowed_licenses`로 설정)가 확인된 논문만 후보로 골라낸다. "공개
여부만 믿고 원문을 저장하지 않고, 재사용 가능한 라이선스가 확인된 PDF만 수집한다"는
docs/guide/11-paper-collection.md의 원칙을 API 필터(`best_oa_location.license`)와 응답 파싱
단계 이중으로 강제한다. 실제 다운로드는 이 클라이언트가 아니라 `service.py`(`PaperCollector`)가
수행한다 — 이 모듈은 "무엇을 받을지"만 결정한다.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import httpx

from paperrag.collect.models import PaperCandidate
from paperrag.config import Settings, get_settings

# OpenAlex 응답에서 필요한 필드만 선택해(select 파라미터) 응답 크기를 줄이고 파싱 대상을 명확히 한다.
WORK_SELECT_FIELDS = ",".join(
    (
        "id",
        "doi",
        "title",
        "publication_year",
        "language",
        "authorships",
        "best_oa_location",
        "is_retracted",
    )
)


class PaperDiscoveryError(RuntimeError):
    """논문 발견 API 응답을 신뢰 가능한 후보로 변환하지 못함."""


class OpenAlexClient:
    """OpenAlex Works API를 감싸 라이선스 필터링이 적용된 `PaperCandidate` 목록을 반환하는 클라이언트."""

    def __init__(
        self,
        settings: Settings | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        # 테스트 등에서 httpx.Client를 주입할 수 있게 하되, 주입된 경우에는 이 클래스가 소유자가
        # 아니므로 close()에서 닫지 않는다(호출자가 생명주기를 관리).
        self._owns_client = client is None
        self.client = client or httpx.Client(
            timeout=self.settings.paper_collection_timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": self._user_agent()},
        )

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def search(
        self,
        query: str,
        limit: int,
        *,
        language: str | None = None,
    ) -> list[PaperCandidate]:
        """자연어 검색어로 OpenAlex Works를 검색해 라이선스 필터를 통과한 후보 최대 `limit`편을 반환한다.

        `language`(ISO 639-1 코드, 예: "ko")를 주면 OpenAlex의 `language` 필터를 추가해 해당
        언어로 표기된 논문만 조회한다 — 검색어를 한국어로 써도 OpenAlex 자체 관련도 정렬은
        언어와 무관하게 동작해 영문 논문이 섞여 나올 수 있으므로, 특정 언어 표본이 필요할 때는
        검색어와 별개로 이 필터를 함께 써야 한다.
        """
        if not query.strip():
            raise ValueError("논문 검색어는 비어 있을 수 없습니다.")
        if limit < 1:
            raise ValueError("수집 개수는 1 이상이어야 합니다.")
        allowed = self._allowed_licenses()
        if not allowed:
            raise ValueError("허용할 논문 라이선스를 하나 이상 설정해야 합니다.")
        # API 필터로 걸러지지 않는 사유(예: PDF URL이 https가 아님)로 파싱 단계에서 추가로 탈락하는
        # 후보가 있으므로, 최종 limit보다 몇 배 넉넉히(candidate_multiplier) 조회해 둔다.
        # 다만 OpenAlex 페이지당 최대 100건 제한을 넘지 않도록 min(100, ...)으로 캡을 씌운다.
        candidate_limit = min(
            100,
            max(limit, limit * self.settings.paper_collection_candidate_multiplier),
        )
        # is_oa: 공개 접근본이 있어야 함 / has_pdf_url: 실제 PDF 링크가 있어야 함 /
        # is_retracted:false: 철회 논문 제외 / license: 허용 라이선스만(OR 조건, '|').
        filter_parts = [
            "is_oa:true",
            "has_pdf_url:true",
            "is_retracted:false",
            f"best_oa_location.license:{'|'.join(sorted(allowed))}",
        ]
        if language and language.strip():
            filter_parts.append(f"language:{language.strip().lower()}")
        params = self._auth_params()
        params.update(
            {
                "search": query.strip(),
                "filter": ",".join(filter_parts),
                "per-page": str(candidate_limit),
                "select": WORK_SELECT_FIELDS,
            }
        )
        payload = self._get_json("/works", params)
        results = payload.get("results")
        if not isinstance(results, list):
            raise PaperDiscoveryError("OpenAlex 응답에 results 배열이 없습니다.")
        candidates = self._parse_candidates(results)
        # API 필터를 통과해도 파싱 단계 검증에서 일부가 탈락할 수 있으므로, 넉넉히 받은 뒤 여기서
        # 최종적으로 사용자가 요청한 개수만큼만 잘라 반환한다.
        return candidates[:limit]

    def get_works(self, work_ids: Sequence[str]) -> list[PaperCandidate]:
        """고정된 OpenAlex work ID 목록을 그대로 조회한다(동일 테스트셋 재현용).

        검색과 달리 여기서는 후보가 라이선스 조건을 만족하지 않으면 조용히 건너뛰지 않고
        `PaperDiscoveryError`를 던진다 — 특정 논문을 명시적으로 지정했는데 조건 미달로 조용히
        빠지면 재현성이 깨지기 때문이다.
        """
        candidates: list[PaperCandidate] = []
        for work_id in work_ids:
            normalized = _normalize_work_id(work_id)
            payload = self._get_json(
                f"/works/{normalized}",
                {**self._auth_params(), "select": WORK_SELECT_FIELDS},
            )
            candidate = self._parse_candidate(payload)
            if candidate is None:
                raise PaperDiscoveryError(
                    f"{normalized}은 허용 라이선스의 공개 PDF가 아닙니다."
                )
            candidates.append(candidate)
        return candidates

    def _get_json(self, path: str, params: Mapping[str, str]) -> Mapping[str, Any]:
        """OpenAlex에 GET 요청을 보내고 JSON 객체로 파싱한다. 네트워크·파싱 실패는 모두
        `PaperDiscoveryError`로 통일해, 호출부가 OpenAlex 내부 예외 타입을 알 필요 없게 한다."""
        try:
            response = self.client.get(
                f"{self.settings.openalex_base_url.rstrip('/')}{path}",
                params=params,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise PaperDiscoveryError(f"OpenAlex 요청 실패: {exc}") from exc
        if not isinstance(payload, Mapping):
            raise PaperDiscoveryError("OpenAlex 응답이 JSON 객체가 아닙니다.")
        return payload

    def _parse_candidates(self, rows: Sequence[Any]) -> list[PaperCandidate]:
        """검색 결과 배열을 후보로 변환하되, 조건 미달 항목은 조용히 걸러내고 같은
        `source_id`가 중복 등장하면 첫 번째만 유지한다."""
        candidates: list[PaperCandidate] = []
        seen: set[str] = set()
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            candidate = self._parse_candidate(row)
            if candidate is not None and candidate.source_id not in seen:
                candidates.append(candidate)
                seen.add(candidate.source_id)
        return candidates

    def _parse_candidate(self, row: Mapping[str, Any]) -> PaperCandidate | None:
        """OpenAlex work 응답 1건을 검증해 `PaperCandidate`로 변환한다.

        여기서 `None`을 반환하는 모든 경우가 "이 시스템이 저장해도 되는 논문이 아니다"라는 판단
        이다: 철회된 논문, 라이선스가 허용 목록에 없는 경우, PDF/랜딩 페이지 URL이 HTTPS가 아닌
        경우, 필수 필드(ID·제목)가 비어 있는 경우.
        """
        if row.get("is_retracted") is True:
            return None
        location = row.get("best_oa_location")
        if not isinstance(location, Mapping):
            return None
        license_name = str(location.get("license") or "").strip().lower()
        pdf_url = str(location.get("pdf_url") or "").strip()
        landing_page_url = str(location.get("landing_page_url") or "").strip()
        if license_name not in self._allowed_licenses() or not _is_https_url(pdf_url):
            return None
        if not _is_https_url(landing_page_url):
            # 랜딩 페이지가 없거나 http인 경우 PDF URL로 대체한다 — 최소한 출처를 추적할 링크는
            # 남겨야 하므로(provenance 목적), 완전히 비워두지 않는다.
            landing_page_url = pdf_url
        source_id = _normalize_work_id(str(row.get("id") or ""))
        title = " ".join(str(row.get("title") or "").split())
        if not source_id or not title:
            return None
        authors = tuple(_author_names(row.get("authorships")))
        source = location.get("source")
        source_name = (
            str(source.get("display_name") or "").strip()
            if isinstance(source, Mapping)
            else None
        )
        return PaperCandidate(
            source_provider="openalex",
            source_id=source_id,
            title=title,
            authors=authors,
            publication_year=_optional_int(row.get("publication_year")),
            doi=_normalize_doi(row.get("doi")),
            landing_page_url=landing_page_url,
            pdf_url=pdf_url,
            license=license_name,
            language=str(row.get("language") or "").strip() or None,
            source_name=source_name or None,
        )

    def _allowed_licenses(self) -> set[str]:
        """`Settings.paper_collection_allowed_licenses`(콤마 구분 문자열)를 소문자 집합으로 파싱한다."""
        return {
            item.strip().lower()
            for item in self.settings.paper_collection_allowed_licenses.split(",")
            if item.strip()
        }

    def _auth_params(self) -> dict[str, str]:
        """API 키가 설정돼 있으면 요청 파라미터에 포함한다(없어도 익명 요청 자체는 가능).

        docs/guide/11-paper-collection.md 2단계 참고 — 무료 키는 일일 사용량 제한이 있고
        polite pool 대우를 받기 위한 것으로, 익명 요청도 현재는 동작하지만 공식 보장 조건은 아니다.
        """
        return (
            {"api_key": self.settings.openalex_api_key}
            if self.settings.openalex_api_key
            else {}
        )

    def _user_agent(self) -> str:
        """담당자 이메일이 설정돼 있으면 User-Agent에 포함해 OpenAlex의 'polite pool'
        대우(더 안정적인 응답)를 받도록 한다."""
        contact = self.settings.openalex_contact_email
        return f"paper-rag/0.1 (mailto:{contact})" if contact else "paper-rag/0.1"


def _normalize_work_id(value: str) -> str:
    """OpenAlex work ID를 표준 형태("W" + 숫자)로 정규화한다.

    URL 형태(`https://openalex.org/W12345`)로 주어져도 마지막 경로 조각만 취해 처리하고,
    "W"로 시작해 나머지가 전부 숫자가 아니면 명확한 오류로 알린다.
    """
    normalized = value.strip().rstrip("/").rsplit("/", 1)[-1]
    if len(normalized) < 2 or normalized[0].upper() != "W" or not normalized[1:].isdigit():
        raise ValueError(f"올바르지 않은 OpenAlex work ID입니다: {value}")
    return "W" + normalized[1:]


def _author_names(value: Any) -> list[str]:
    """OpenAlex의 `authorships` 배열에서 저자 표시 이름만 순서대로 추출한다."""
    if not isinstance(value, list):
        return []
    names: list[str] = []
    for row in value:
        if not isinstance(row, Mapping):
            continue
        author = row.get("author")
        if isinstance(author, Mapping):
            name = str(author.get("display_name") or "").strip()
            if name:
                names.append(name)
    return names


def _optional_int(value: Any) -> int | None:
    """값을 정수로 변환하되 없거나 변환 불가능하면 None으로 처리해(예외를 던지지 않고) 선택적 필드로 다룬다."""
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _normalize_doi(value: Any) -> str | None:
    """DOI 값에서 `https://doi.org/` 접두사를 제거해 순수 DOI 문자열만 남긴다(중복 제거 STEP 등에서 비교 용이)."""
    normalized = str(value or "").strip()
    if not normalized:
        return None
    return normalized.removeprefix("https://doi.org/").removeprefix("http://doi.org/")


def _is_https_url(value: str) -> bool:
    """PDF/랜딩 페이지 URL이 HTTPS인지 확인한다(평문 HTTP 다운로드를 허용하지 않기 위한 최소 검증)."""
    return value.startswith("https://")
