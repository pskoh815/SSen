import sys, pandas as pd
sys.stdout.reconfigure(encoding='utf-8')
from pathlib import Path

parts = sorted(Path('data/parquet/fact_adr').glob('*/data.parquet'))
print(f'fact_adr 파티션: {len(parts)}')
df = pd.read_parquet(parts[0])
print('컬럼:', df.columns.tolist())
print(df.head(3).to_string())

df_all = pd.concat([pd.read_parquet(p) for p in parts])
df_all['date'] = pd.to_datetime(df_all['date']).dt.date
print(f'\n날짜 범위: {df_all["date"].min()} ~ {df_all["date"].max()}')
print(f'행 수: {len(df_all)}, 컬럼: {df_all.columns.tolist()}')
print(df_all.tail(5).to_string())
