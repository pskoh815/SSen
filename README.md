# SSen

`SSen분석.xlsx` 기반 주도주/테마 분석 대시보드. Parquet/Postgres 파생 데이터 +
FastAPI + HTML 대시보드로 구성됨. 상세 설계는 [CLAUDE.md](CLAUDE.md) 참조.

## 이 저장소에 포함된 것

코드(`src/ssen/`), 대시보드(`apps/dashboard/`), 문서(`docs/`, `CLAUDE*.md`),
실행 스크립트(`run.ps1`, `start_api.bat`)만 포함됩니다.

**포함되지 않은 것** (`.gitignore`로 제외, 로컬에서 직접 준비 필요):

| 항목 | 내용 |
|---|---|
| `data/` | Parquet 파생 데이터 + `catalog.json` — 원본 `SSen분석.xlsx` ingest 또는 백필로 생성 |
| `logs/` | 실행 로그 |
| `.env` | API 키 (`DATA_GO_KR_KEY`, `OPENKRX_KEY`, `OPENDART_KEY`) |
| Postgres/Redis | 별도 Docker 컨테이너로 직접 기동 (아래 참조) |

## 로컬 구동 준비

1. **Python 의존성**
   ```
   pip install -r requirements.txt
   ```

2. **Docker 컨테이너** (Postgres + Redis)
   ```
   docker run -d --name ssen_postgres -e POSTGRES_USER=ssen -e POSTGRES_PASSWORD=ssen ^
     -e POSTGRES_DB=ssen -p 5432:5432 postgres:17-alpine
   docker run -d --name ssen_redis -p 6379:6379 redis:7-alpine
   ```

3. **`.env` 파일 생성** (저장소 루트)
   ```
   DATA_GO_KR_KEY=<공공데이터포털 발급키>
   OPENKRX_KEY=<KRX OPEN API AUTH_KEY>
   OPENDART_KEY=<OpenDART API 키>
   ```

4. **데이터 적재** — 원본 `data/incoming/SSen분석.xlsx`를 준비한 뒤:
   ```powershell
   .\run.ps1 update
   ```

5. **API 서버 기동**
   ```powershell
   .\run.ps1 api_up
   ```

6. **대시보드 열기** — `apps/dashboard/index.html`을 브라우저로 직접 열기
   (API 서버가 `http://localhost:8000`에서 응답 중이어야 함)

## 키움 OpenAPI+ 수집 (선택)

당일 데이터는 키움 OpenAPI+ 경로(32bit Python, `py -3.9-32`)로도 수집 가능.
과거 데이터 백필은 data.go.kr/KRX OPEN API 경로만으로 충분. 상세는
[docs/kiwoom_collection_spec.md](docs/kiwoom_collection_spec.md) 참조.
