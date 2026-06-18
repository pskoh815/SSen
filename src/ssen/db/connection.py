"""DB 연결 헬퍼."""
import os
import psycopg2
from psycopg2.extras import execute_values
from contextlib import contextmanager

DEFAULT_URL = "postgresql://ssen:ssen@127.0.0.1:5432/ssen"


def get_url() -> str:
    return os.environ.get("SSEN_DB_URL", DEFAULT_URL)


@contextmanager
def get_conn():
    conn = psycopg2.connect(get_url())
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def get_cur(conn):
    cur = conn.cursor()
    try:
        yield cur
    finally:
        cur.close()
