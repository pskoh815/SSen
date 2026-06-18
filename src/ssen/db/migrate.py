"""
마이그레이션 실행기.
Usage: python -m ssen.db.migrate [--migrations-dir PATH]
"""
import argparse
import sys
from pathlib import Path
from .connection import get_conn, get_cur

MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "migrations"


def run_migrations(migrations_dir: Path) -> None:
    sql_files = sorted(migrations_dir.glob("*.sql"))
    if not sql_files:
        print(f"No .sql files in {migrations_dir}")
        return

    with get_conn() as conn:
        with get_cur(conn) as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version    TEXT PRIMARY KEY,
                    applied_at TIMESTAMPTZ DEFAULT now()
                )
            """)
            conn.commit()

        for sql_file in sql_files:
            version = sql_file.stem
            with get_cur(conn) as cur:
                cur.execute("SELECT 1 FROM schema_migrations WHERE version = %s", (version,))
                if cur.fetchone():
                    print(f"  skip {version} (already applied)")
                    continue

            sql = sql_file.read_text(encoding="utf-8")
            with get_cur(conn) as cur:
                cur.execute(sql)
                cur.execute("INSERT INTO schema_migrations(version) VALUES(%s)", (version,))
            conn.commit()
            print(f"  applied {version}")

    print("Migrations complete.")


def main():
    parser = argparse.ArgumentParser(description="Run SQL migrations")
    parser.add_argument("--migrations-dir", default=str(MIGRATIONS_DIR))
    args = parser.parse_args()
    run_migrations(Path(args.migrations_dir))


if __name__ == "__main__":
    main()
