from pathlib import Path
import sys
from collections.abc import Callable

import psycopg

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from paperrag.config import get_settings  # noqa: E402


def _psycopg_dsn(database_url: str) -> str:
    return database_url.replace("postgresql+psycopg://", "postgresql://", 1)


def _migration_files() -> list[Path]:
    return sorted((PROJECT_ROOT / "db" / "migrations").glob("*.sql"))


def apply_migrations(dsn: str, migrations_dir: Path) -> list[str]:
    return _apply_migrations(dsn, migrations_dir, log=None)


def _apply_migrations(
    dsn: str,
    migrations_dir: Path,
    *,
    log: Callable[[str], None] | None,
) -> list[str]:
    migrations = sorted(migrations_dir.glob("*.sql"))
    applied_now: list[str] = []

    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                filename TEXT PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        applied = {
            row[0]
            for row in connection.execute(
                "SELECT filename FROM schema_migrations ORDER BY filename"
            ).fetchall()
        }

        for migration in migrations:
            filename = migration.name
            if filename in applied:
                if log is not None:
                    log(f"skip {filename}")
                continue

            sql = migration.read_text(encoding="utf-8")
            with connection.transaction():
                connection.execute(sql)
                connection.execute(
                    "INSERT INTO schema_migrations (filename, applied_at) VALUES (%s, now())",
                    (filename,),
                )
            applied_now.append(filename)
            if log is not None:
                log(f"apply {filename}")

    return applied_now


def main() -> int:
    settings = get_settings()
    dsn = _psycopg_dsn(settings.database_url)
    migrations = _migration_files()

    if not migrations:
        print("no migration files found")
        return 0

    _apply_migrations(dsn, PROJECT_ROOT / "db" / "migrations", log=print)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
