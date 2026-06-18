"""월 파티션 자동 생성."""
import calendar
from .connection import get_conn, get_cur

PARTITIONED_TABLES = ["fact_daily_stock", "fact_kospi_index", "fact_adr"]


def _partition_name(table: str, yearmonth: str) -> str:
    return f"{table}_{yearmonth}"


def ensure_partition(conn, table: str, yearmonth: str) -> bool:
    """파티션이 없으면 생성. 이미 있으면 skip. 반환: 생성 여부."""
    year = int(yearmonth[:4])
    month = int(yearmonth[4:])
    _, last_day = calendar.monthrange(year, month)
    start = f"{year:04d}-{month:02d}-01"
    # end는 다음달 1일 (exclusive)
    if month == 12:
        end = f"{year+1:04d}-01-01"
    else:
        end = f"{year:04d}-{month+1:02d}-01"

    part_name = _partition_name(table, yearmonth)
    with get_cur(conn) as cur:
        cur.execute(
            "SELECT 1 FROM pg_class WHERE relname = %s AND relkind = 'r'",
            (part_name,)
        )
        if cur.fetchone():
            return False
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {part_name}
            PARTITION OF {table}
            FOR VALUES FROM ('{start}') TO ('{end}')
        """)
    return True


def ensure_partitions_for_months(yearmonths: list[str]) -> dict[str, list[str]]:
    """여러 테이블에 대해 yearmonths 파티션을 일괄 생성."""
    created: dict[str, list[str]] = {t: [] for t in PARTITIONED_TABLES}
    with get_conn() as conn:
        for ym in sorted(set(yearmonths)):
            for table in PARTITIONED_TABLES:
                if ensure_partition(conn, table, ym):
                    created[table].append(ym)
                    print(f"  created partition {table}_{ym}")
        conn.commit()
    return created
