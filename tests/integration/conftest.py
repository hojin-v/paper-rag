import os
from pathlib import Path
import sys
from typing import Iterator

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

_RUNTIME_DIR = Path(os.environ.get("PAPERRAG_PGSERVER_RUNTIME_DIR", "/tmp/paperrag-pgserver"))
_RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
os.environ["XDG_RUNTIME_DIR"] = str(_RUNTIME_DIR)

pgserver = pytest.importorskip("pgserver")

from scripts.apply_migrations import apply_migrations  # noqa: E402


@pytest.fixture(scope="session")
def pg_dsn(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    try:
        db = pgserver.get_server(tmp_path_factory.mktemp("pg"))
    except OSError as exc:
        pytest.skip(f"pgserver cannot start in this environment: {exc}")

    try:
        dsn = db.get_uri()
        apply_migrations(dsn, PROJECT_ROOT / "db" / "migrations")
        yield dsn
    finally:
        db.cleanup()
