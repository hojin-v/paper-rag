from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx

from paperrag.collect.openalex import OpenAlexClient, PaperDiscoveryError
from paperrag.collect.service import PaperCollector, lookup_source_metadata
from paperrag.config import Settings


def _work(
    *,
    work_id: str = "W123",
    license_name: str = "cc-by",
    pdf_url: str = "https://papers.example/paper.pdf",
) -> dict[str, Any]:
    return {
        "id": f"https://openalex.org/{work_id}",
        "doi": "https://doi.org/10.1000/example",
        "title": "Document Layout Analysis",
        "publication_year": 2025,
        "language": "en",
        "is_retracted": False,
        "authorships": [
            {"author": {"display_name": "A. Researcher"}},
            {"author": {"display_name": "B. Scientist"}},
        ],
        "best_oa_location": {
            "landing_page_url": "https://papers.example/article",
            "pdf_url": pdf_url,
            "license": license_name,
            "source": {"display_name": "Example Journal"},
        },
    }


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        paper_collection_dir=tmp_path,
        openalex_base_url="https://api.openalex.test",
        paper_collection_allowed_licenses="cc-by,cc-by-sa,cc0",
    )


def test_openalex_search_filters_license_and_maps_candidates(tmp_path: Path) -> None:
    captured_query: dict[str, list[str]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_query.update(parse_qs(request.url.query.decode()))
        return httpx.Response(
            200,
            json={
                "results": [
                    _work(),
                    _work(work_id="W456", license_name="cc-by-nc-nd"),
                ]
            },
        )

    settings = _settings(tmp_path)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    discovery = OpenAlexClient(settings, client)

    candidates = discovery.search("document OCR", 2)

    assert [candidate.source_id for candidate in candidates] == ["W123"]
    assert candidates[0].doi == "10.1000/example"
    assert candidates[0].authors == ("A. Researcher", "B. Scientist")
    assert "best_oa_location.license:cc-by|cc-by-sa|cc0" in captured_query["filter"][0]


def test_openalex_search_adds_language_filter_when_given(tmp_path: Path) -> None:
    captured_query: dict[str, list[str]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_query.update(parse_qs(request.url.query.decode()))
        return httpx.Response(200, json={"results": [_work()]})

    settings = _settings(tmp_path)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    discovery = OpenAlexClient(settings, client)

    discovery.search("문서 레이아웃 분석", 1, language="ko")

    assert "language:ko" in captured_query["filter"][0]


def test_collects_pdf_writes_provenance_and_skips_verified_duplicate(
    tmp_path: Path,
) -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(
            200,
            content=b"%PDF-1.7\ncollection-test",
            headers={"Content-Type": "application/pdf"},
        )

    settings = _settings(tmp_path)
    candidate = OpenAlexClient(
        settings,
        httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(500))),
    )._parse_candidate(_work())
    assert candidate is not None
    collector = PaperCollector(
        OpenAlexClient(
            settings,
            httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(500))),
        ),
        settings,
        httpx.Client(transport=httpx.MockTransport(handler)),
    )

    first = collector.collect_candidates([candidate])
    second = collector.collect_candidates([candidate])

    assert len(first.downloaded) == 1
    assert len(second.skipped) == 1
    assert requests == 1
    pdf_path = Path(first.downloaded[0].local_path)
    assert pdf_path.read_bytes().startswith(b"%PDF-")
    records = [
        json.loads(line)
        for line in (tmp_path / "collection-manifest.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert len(records) == 1
    assert records[0]["license"] == "cc-by"
    assert records[0]["sha256"] == first.downloaded[0].sha256


def test_rejects_non_pdf_response_without_manifest(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    discovery_client = httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=_work()))
    )
    discovery = OpenAlexClient(settings, discovery_client)
    candidate = discovery.get_works(["W123"])[0]
    download_client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, text="publisher error page")
        )
    )
    collector = PaperCollector(discovery, settings, download_client)

    report = collector.collect_candidates([candidate])

    assert report.downloaded == []
    assert report.failures[0][0] == "W123"
    assert "PDF 시그니처" in report.failures[0][1]
    assert not list(tmp_path.glob("*.pdf"))
    assert not (tmp_path / "collection-manifest.jsonl").exists()


def test_get_work_rejects_unknown_or_restrictive_license(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=_work(license_name="other-oa"))
        )
    )
    discovery = OpenAlexClient(settings, client)

    try:
        discovery.get_works(["W123"])
    except PaperDiscoveryError as exc:
        assert "허용 라이선스" in str(exc)
    else:
        raise AssertionError("제한 라이선스 논문이 수집 후보로 허용됨")


def test_lookup_source_metadata_returns_journal_and_link(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    pdf_path = tmp_path / "W123-paper.pdf"
    pdf_path.write_bytes(b"%PDF-1.7\n")
    (tmp_path / "collection-manifest.jsonl").write_text(
        json.dumps(
            {
                "source_id": "W123",
                "sha256": "abc",
                "local_path": str(pdf_path),
                "source_name": "Example Journal",
                "landing_page_url": "https://papers.example/article",
                "pdf_url": "https://papers.example/paper.pdf",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    journal, link = lookup_source_metadata(pdf_path, settings)

    assert journal == "Example Journal"
    # landing_page_url을 우선 사용한다.
    assert link == "https://papers.example/article"


def test_lookup_source_metadata_missing_manifest_returns_none(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    assert lookup_source_metadata(tmp_path / "unknown.pdf", settings) == (None, None)


def test_lookup_source_metadata_falls_back_to_source_id_in_filename(tmp_path: Path) -> None:
    """검수 워크플로우는 업로드 파일을 source.pdf로 복사·개명해 경로/파일명이 더 이상

    manifest의 local_path와 일치하지 않는다 — 원본 파일명(collect가 부여한
    "{source_id}-슬러그.pdf" 패턴을 그대로 담고 있는 경우가 많음)에 남은 source_id
    토큰만으로도 manifest를 찾을 수 있어야 한다(review.service.ReviewService.ingest가
    document.filename을 넘겨 호출하는 경로).
    """
    settings = _settings(tmp_path)
    original_pdf = tmp_path / "W4226020328-lilt-a-simple-yet-effective.pdf"
    original_pdf.write_bytes(b"%PDF-1.7\n")
    (tmp_path / "collection-manifest.jsonl").write_text(
        json.dumps(
            {
                "source_id": "W4226020328",
                "sha256": "abc",
                "local_path": str(original_pdf),
                "source_name": "ACL",
                "landing_page_url": "https://doi.org/example",
                "pdf_url": "https://example.test/paper.pdf",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    # 검수 저장소가 저장한 파일명(경로도 다르고 이름도 원본과 다름)이지만, 원본 업로드
    # 파일명("consistency-v2-W4226020328-full-11p.pdf")을 대신 넘긴다.
    journal, link = lookup_source_metadata(
        "consistency-v2-W4226020328-full-11p.pdf", settings
    )

    assert journal == "ACL"
    assert link == "https://doi.org/example"


def test_lookup_source_metadata_no_source_id_token_returns_none(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    (tmp_path / "collection-manifest.jsonl").write_text(
        json.dumps(
            {
                "source_id": "W4226020328",
                "sha256": "abc",
                "local_path": str(tmp_path / "W4226020328-lilt.pdf"),
                "source_name": "ACL",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    assert lookup_source_metadata("source.pdf", settings) == (None, None)
