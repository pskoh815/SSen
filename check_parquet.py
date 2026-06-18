import duckdb, glob
files = glob.glob('data/parquet/**/*.parquet', recursive=True)
print('parquet files found:', len(files))
if files:
    con = duckdb.connect()
    f = files[0].replace('\\','/')
    schema = con.execute(f"DESCRIBE SELECT * FROM read_parquet('{f}') LIMIT 1").fetchall()
    for col in schema: print(col)
    sample = con.execute(f"SELECT * FROM read_parquet('{f}') LIMIT 3").fetchdf()
    print(sample.to_string())
else:
    print("No parquet files found")
    import os
    for root, dirs, fns in os.walk('data'):
        for fn in fns:
            print(os.path.join(root,fn))
