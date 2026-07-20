from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from paperrag.collect import cli
from paperrag.collect.models import DownloadedPaper, PaperCandidate


class _FakeDelayTask:
    """Celery 태스크의 `.delay(...)`만 흉내 내는 페이크. 호출 인자를 기록한다."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def delay(self, *args: Any) -> None:
        self.calls.append(args)


class _FakeReport:
    """CollectionReport를 흉내 내는 페이크(다운로드/스킵/실패 목록만 담음)."""

    def __init__(
        self,
        downloaded: list[DownloadedPaper] | None = None,
        skipped: list[DownloadedPaper] | None = None,
        failures: list[tuple[str, str]] | None = None,
    ) -> None:
        self.downloaded = downloaded or []
        self.skipped = skipped or []
        self.failures = failures or []


def test_enqueue_ingest_calls_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    """_enqueue_ingest가 ingest_collected_paper.delay(source_path)를 호출해야 한다."""
    fake_task = _FakeDelayTask()
    fake_module = type("_FakeWorkerApp", (), {"ingest_collected_paper": fake_task})()
    monkeypatch.setitem(
        __import__("sys").modules, "paperrag.worker.app", fake_module
    )

    cli._enqueue_ingest("/tmp/paper.pdf")

    assert fake_task.calls == [("/tmp/paper.pdf",)]


def test_enqueue_ingest_swallows_errors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """워커/브로커가 없어 큐 등록이 실패해도 예외를 밖으로 던지지 않고 경고만 출력한다."""

    class _RaisingTask:
        def delay(self, *args: Any) -> None:
            raise RuntimeError("broker unreachable")

    fake_module = type("_FakeWorkerApp", (), {"ingest_collected_paper": _RaisingTask()})()
    monkeypatch.setitem(__import__("sys").modules, "paperrag.worker.app", fake_module)

    cli._enqueue_ingest("/tmp/paper.pdf")  # 예외가 전파되면 이 테스트 자체가 실패한다

    captured = capsys.readouterr()
    assert "경고" in captured.out
    assert "/tmp/paper.pdf" in captured.out


def test_main_enqueues_only_newly_downloaded_papers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """다운로드된 논문만 큐에 넣고, 이미 있던(skipped) 논문은 다시 큐에 넣지 않는다."""
    enqueued: list[str] = []
    monkeypatch.setattr(cli, "_enqueue_ingest", enqueued.append)

    candidate = PaperCandidate(
        source_provider="openalex",
        source_id="W1",
        title="Test Paper",
        authors=("A. Author",),
        publication_year=2025,
        doi=None,
        landing_page_url="https://example.test/landing",
        pdf_url="https://example.test/paper.pdf",
        license="cc-by",
        language="en",
        source_name="Example Journal",
    )
    downloaded = DownloadedPaper(
        candidate=candidate,
        local_path=str(tmp_path / "W1-test-paper.pdf"),
        sha256="deadbeef",
        byte_size=100,
        retrieved_at="2026-07-20T00:00:00Z",
        status="downloaded",
    )
    skipped_candidate = PaperCandidate(
        source_provider="openalex",
        source_id="W2",
        title="Already Collected",
        authors=("B. Author",),
        publication_year=2024,
        doi=None,
        landing_page_url="https://example.test/landing2",
        pdf_url="https://example.test/paper2.pdf",
        license="cc-by",
        language="en",
        source_name="Example Journal",
    )
    skipped = DownloadedPaper(
        candidate=skipped_candidate,
        local_path=str(tmp_path / "W2-already-collected.pdf"),
        sha256="cafebabe",
        byte_size=100,
        retrieved_at="2026-07-19T00:00:00Z",
        status="skipped",
    )

    report = _FakeReport(downloaded=[downloaded], skipped=[skipped])

    class _FakeCollector:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def collect_candidates(self, candidates: Any, output_dir: Any = None) -> "_FakeReport":
            return report

        def close(self) -> None:
            pass

    class _FakeDiscovery:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def search(
            self, query: str, limit: int, *, language: str | None = None
        ) -> list[PaperCandidate]:
            return [candidate]

    monkeypatch.setattr(cli, "OpenAlexClient", _FakeDiscovery)
    monkeypatch.setattr(cli, "PaperCollector", _FakeCollector)

    exit_code = cli.main(["--query", "test", "--output", str(tmp_path)])

    assert exit_code == 0
    assert enqueued == [downloaded.local_path]
