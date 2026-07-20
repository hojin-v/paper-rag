"""`db/migrations/*.sql`을 순서대로 적용해 PostgreSQL+pgvector 스키마를 최신 상태로 맞추는 스크립트.

docs/guide/03-database.md에서 설명하는 `schema_migrations` 이력 테이블을 이용해 이미 적용한
파일은 건너뛰고(idempotent), 아직 적용하지 않은 파일만 파일명 오름차순으로 실행한다. `make migrate`가
이 스크립트를 호출하며, PostgreSQL 컨테이너가 먼저 기동되어 있어야 한다.
"""

from pathlib import Path
import sys
from collections.abc import Callable

import psycopg

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    # scripts/를 패키지로 설치하지 않고 리포지토리 루트에서 직접 실행하는 것을 전제로 하므로,
    # src/ 레이아웃의 paperrag 패키지를 import하려면 경로를 수동으로 추가해야 한다.
    sys.path.insert(0, str(SRC_ROOT))

from paperrag.config import get_settings  # noqa: E402


def _psycopg_dsn(database_url: str) -> str:
    """SQLAlchemy용 DSN(`postgresql+psycopg://...`)을 psycopg가 바로 쓸 수 있는 형태로 바꾼다."""
    return database_url.replace("postgresql+psycopg://", "postgresql://", 1)


def _migration_files() -> list[Path]:
    """`db/migrations/` 아래 `*.sql` 파일을 파일명 오름차순(=적용 순서)으로 나열한다."""
    return sorted((PROJECT_ROOT / "db" / "migrations").glob("*.sql"))


def apply_migrations(dsn: str, migrations_dir: Path) -> list[str]:
    """외부(테스트 등)에서 로그 없이 마이그레이션만 적용하고 싶을 때 쓰는 공개 진입점."""
    return _apply_migrations(dsn, migrations_dir, log=None)


def _apply_migrations(
    dsn: str,
    migrations_dir: Path,
    *,
    log: Callable[[str], None] | None,
) -> list[str]:
    """미적용 마이그레이션 파일들을 파일명 순서대로, 파일 1개당 트랜잭션 1개로 적용한다.

    각 파일은 SQL 실행과 `schema_migrations`에 적용 기록을 남기는 것을 하나의 트랜잭션으로
    묶어, 파일 도중 오류가 나면 해당 파일의 변경만 롤백되고 이미 적용 완료된 이전 파일들은
    영향을 받지 않는다. 반환값은 이번 호출에서 새로 적용된 파일명 목록이다.
    """
    migrations = sorted(migrations_dir.glob("*.sql"))
    applied_now: list[str] = []

    # autocommit=True: 이력 테이블 생성·조회는 별도 트랜잭션 없이 즉시 반영되어야 하고,
    # 실제 적용 트랜잭션은 아래에서 파일 단위로 개별적으로 연다.
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
                # 이미 적용된 파일은 재실행하지 않는다 — 재실행 시 중복 스키마 오류를 막기 위함.
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
    """`.env`/`Settings`의 DSN으로 접속해 `db/migrations/`의 미적용 파일을 모두 적용하고 진행 로그를 출력한다."""
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
