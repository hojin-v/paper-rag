from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import httpx

from paperrag.collect.models import PaperCandidate
from paperrag.config import Settings, get_settings

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
    def __init__(
        self,
        settings: Settings | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._owns_client = client is None
        self.client = client or httpx.Client(
            timeout=self.settings.paper_collection_timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": self._user_agent()},
        )

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def search(self, query: str, limit: int) -> list[PaperCandidate]:
        if not query.strip():
            raise ValueError("논문 검색어는 비어 있을 수 없습니다.")
        if limit < 1:
            raise ValueError("수집 개수는 1 이상이어야 합니다.")
        allowed = self._allowed_licenses()
        if not allowed:
            raise ValueError("허용할 논문 라이선스를 하나 이상 설정해야 합니다.")
        candidate_limit = min(
            100,
            max(limit, limit * self.settings.paper_collection_candidate_multiplier),
        )
        params = self._auth_params()
        params.update(
            {
                "search": query.strip(),
                "filter": (
                    "is_oa:true,has_pdf_url:true,is_retracted:false,"
                    f"best_oa_location.license:{'|'.join(sorted(allowed))}"
                ),
                "per-page": str(candidate_limit),
                "select": WORK_SELECT_FIELDS,
            }
        )
        payload = self._get_json("/works", params)
        results = payload.get("results")
        if not isinstance(results, list):
            raise PaperDiscoveryError("OpenAlex 응답에 results 배열이 없습니다.")
        candidates = self._parse_candidates(results)
        return candidates[:limit]

    def get_works(self, work_ids: Sequence[str]) -> list[PaperCandidate]:
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
        return {
            item.strip().lower()
            for item in self.settings.paper_collection_allowed_licenses.split(",")
            if item.strip()
        }

    def _auth_params(self) -> dict[str, str]:
        return (
            {"api_key": self.settings.openalex_api_key}
            if self.settings.openalex_api_key
            else {}
        )

    def _user_agent(self) -> str:
        contact = self.settings.openalex_contact_email
        return f"paper-rag/0.1 (mailto:{contact})" if contact else "paper-rag/0.1"


def _normalize_work_id(value: str) -> str:
    normalized = value.strip().rstrip("/").rsplit("/", 1)[-1]
    if len(normalized) < 2 or normalized[0].upper() != "W" or not normalized[1:].isdigit():
        raise ValueError(f"올바르지 않은 OpenAlex work ID입니다: {value}")
    return "W" + normalized[1:]


def _author_names(value: Any) -> list[str]:
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
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _normalize_doi(value: Any) -> str | None:
    normalized = str(value or "").strip()
    if not normalized:
        return None
    return normalized.removeprefix("https://doi.org/").removeprefix("http://doi.org/")


def _is_https_url(value: str) -> bool:
    return value.startswith("https://")
