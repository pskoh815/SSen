import sys
sys.stdout.reconfigure(encoding='utf-8')
from ssen.db.connection import get_conn, get_cur

with get_conn() as conn:
    with get_cur(conn) as cur:
        cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public' ORDER BY table_name")
        print('테이블:')
        for r in cur.fetchall():
            print(' ', r[0])

        cur.execute("SELECT column_name, data_type FROM information_schema.columns WHERE table_name='fact_daily_stock' ORDER BY ordinal_position")
        print('\nfact_daily_stock 컬럼:')
        for r in cur.fetchall():
            print(' ', r[0], '-', r[1])

        cur.execute("SELECT MIN(date), MAX(date), COUNT(DISTINCT date) FROM fact_daily_stock")
        r = cur.fetchone()
        print(f'\n데이터 범위: {r[0]} ~ {r[1]}, {r[2]}거래일')

        # 상승/하락 종목수 관련 컬럼 있는지
        cur.execute("""
            SELECT table_name, column_name FROM information_schema.columns
            WHERE column_name ILIKE '%advanc%' OR column_name ILIKE '%declin%'
               OR column_name ILIKE '%adr%' OR column_name ILIKE '%breadth%'
               OR column_name ILIKE '%상승%' OR column_name ILIKE '%하락%'
            ORDER BY table_name, column_name
        """)
        rows = cur.fetchall()
        print('\nADR/Breadth 관련 컬럼:')
        for r in rows:
            print(' ', r)
