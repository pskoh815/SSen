import duckdb
con = duckdb.connect()

for tbl, path in [
    ('fact_daily_stock', 'data/parquet/fact_daily_stock/**/*.parquet'),
    ('fact_adr',         'data/parquet/fact_adr/**/*.parquet'),
    ('fact_kospi',       'data/parquet/fact_kospi/**/*.parquet'),
]:
    print(f"\n=== {tbl} ===")
    try:
        schema = con.execute(f"DESCRIBE SELECT * FROM read_parquet('{path}') LIMIT 1").fetchall()
        for c in schema:
            print(f"  {c[0]:30s} {c[1]}")
        row = con.execute(f"SELECT * FROM read_parquet('{path}') LIMIT 1").fetchdf()
        print(row.to_string())
    except Exception as e:
        print(f"  ERROR: {e}")
