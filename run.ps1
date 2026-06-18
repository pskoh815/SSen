# SSen Dashboard - Windows PowerShell 실행 스크립트
param(
    [Parameter(Position=0)]
    [ValidateSet("e1_convert","e1_validate","e2_migrate","e2_load","e2_smoketest",
                 "e3_backtest_default","e3_backtest_conservative",
                 "api_up","ui_up","api_bg","api_down","update","help")]
    [string]$Target = "help",

    [ValidateSet("rebuild","new_only")]
    [string]$Overlap = "rebuild",

    [string]$StartDate = "",
    [string]$EndDate   = "",
    [switch]$DryRun
)

$env:PYTHONPATH = "src"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

function Invoke-E1Convert {
    $dryOpt = if ($DryRun) { "--dry-run" } else { "" }
    Write-Host "=== E1: Excel -> Parquet 변환 ===" -ForegroundColor Cyan
    python -m ssen.etl.convert_excel_to_parquet `
        --input-dir data/incoming --output-dir data/parquet `
        --overlap $Overlap $dryOpt
}

function Invoke-E1Validate {
    Write-Host "=== E1: Parquet 검증 ===" -ForegroundColor Cyan
    python -m ssen.etl.validate_parquet --output-dir data/parquet --report-format both
}

function Invoke-E2Migrate {
    Write-Host "=== E2: DB 마이그레이션 ===" -ForegroundColor Cyan
    python -m ssen.db.migrate
}

function Invoke-E2Load {
    Write-Host "=== E2: Parquet -> Postgres 적재 ===" -ForegroundColor Cyan
    $months = if ($StartDate) { "--months $StartDate" } else { "" }
    python -m ssen.db.load_parquet_to_postgres --parquet-dir data/parquet --overlap $Overlap $months
}

function Invoke-E2Smoketest {
    Write-Host "=== E2: Smoketest ===" -ForegroundColor Cyan
    python -m ssen.db.smoketest
}

function Invoke-E3Backtest([string]$rule) {
    Write-Host "=== E3: 파생 테이블 계산 ($rule) ===" -ForegroundColor Cyan
    $sd = if ($StartDate) { "--start-date $StartDate" } else { "" }
    $ed = if ($EndDate)   { "--end-date $EndDate" }     else { "" }
    python -m ssen.strategy.backtest --rule $rule $sd $ed
}

function Invoke-ApiUp {
    Write-Host "=== API 서버 시작 ===" -ForegroundColor Cyan
    Write-Host "Swagger UI: http://localhost:8000/docs" -ForegroundColor Green
    Write-Host "대시보드:   http://localhost:8000/dashboard" -ForegroundColor Green
    python -m uvicorn ssen.api.main:app --host 0.0.0.0 --port 8000 --reload
}

$pidFile = Join-Path $root ".run\api.pid"

function Invoke-ApiBg {
    New-Item -ItemType Directory -Force -Path (Split-Path $pidFile) | Out-Null

    if (Test-Path $pidFile) {
        $oldPid = Get-Content $pidFile -ErrorAction SilentlyContinue
        if ($oldPid -and (Get-Process -Id $oldPid -ErrorAction SilentlyContinue)) {
            Write-Host "API 서버가 이미 실행 중입니다 (PID $oldPid)." -ForegroundColor Yellow
            Write-Host "대시보드:   http://localhost:8000/dashboard" -ForegroundColor Green
            return
        }
    }

    Write-Host "=== API 서버 백그라운드 시작 ===" -ForegroundColor Cyan
    $proc = Start-Process -FilePath "python" `
        -ArgumentList "-m", "uvicorn", "ssen.api.main:app", "--host", "0.0.0.0", "--port", "8000" `
        -WorkingDirectory $root -WindowStyle Hidden -PassThru
    Set-Content -Path $pidFile -Value $proc.Id
    Write-Host "PID: $($proc.Id) (기록: $pidFile)" -ForegroundColor Green
    Write-Host "Swagger UI: http://localhost:8000/docs" -ForegroundColor Green
    Write-Host "대시보드:   http://localhost:8000/dashboard" -ForegroundColor Green
}

function Invoke-ApiDown {
    if (-not (Test-Path $pidFile)) {
        Write-Host "PID 파일이 없습니다 (api_bg로 시작한 서버가 없는 것 같습니다)." -ForegroundColor Yellow
        return
    }
    $targetPid = Get-Content $pidFile -ErrorAction SilentlyContinue
    if ($targetPid -and (Get-Process -Id $targetPid -ErrorAction SilentlyContinue)) {
        Stop-Process -Id $targetPid -Force
        Write-Host "API 서버(PID $targetPid)를 종료했습니다." -ForegroundColor Green
    } else {
        Write-Host "PID $targetPid 프로세스가 이미 종료되어 있습니다." -ForegroundColor Yellow
    }
    Remove-Item $pidFile -Force
}

switch ($Target) {
    "e1_convert"               { Invoke-E1Convert }
    "e1_validate"              { Invoke-E1Validate }
    "e2_migrate"               { Invoke-E2Migrate }
    "e2_load"                  { Invoke-E2Load }
    "e2_smoketest"             { Invoke-E2Smoketest }
    "e3_backtest_default"      { Invoke-E3Backtest "default" }
    "e3_backtest_conservative" { Invoke-E3Backtest "conservative" }
    "api_up"                   { Invoke-ApiUp }
    "ui_up"                    { Invoke-ApiUp }
    "api_bg"                   { Invoke-ApiBg }
    "api_down"                 { Invoke-ApiDown }
    "update" {
        Invoke-E1Convert
        if ($LASTEXITCODE -eq 0) { Invoke-E1Validate }
        if ($LASTEXITCODE -eq 0) { Invoke-E2Load }
        if ($LASTEXITCODE -eq 0) { Invoke-E2Smoketest }
        if ($LASTEXITCODE -eq 0) { Invoke-E3Backtest "default" }
        Write-Host "=== E-UPDATE 완료 ===" -ForegroundColor Green
    }
    "help" {
        Write-Host "Usage: .\run.ps1 <target> [options]" -ForegroundColor Yellow
        Write-Host ""
        Write-Host "Targets:"
        Write-Host "  e1_convert               Excel -> Parquet 변환"
        Write-Host "  e1_validate              Parquet 품질 검증"
        Write-Host "  e2_migrate               DB 마이그레이션"
        Write-Host "  e2_load                  Parquet -> Postgres 적재"
        Write-Host "  e2_smoketest             DB 쿼리 5개 검증"
        Write-Host "  e3_backtest_default      파생 테이블 계산 (기본 룰)"
        Write-Host "  e3_backtest_conservative 파생 테이블 계산 (보수적 룰)"
        Write-Host "  api_up / ui_up           FastAPI 서버 + 대시보드 시작 (포그라운드, --reload)"
        Write-Host "  api_bg                   FastAPI 서버를 백그라운드(숨김 프로세스)로 시작"
        Write-Host "  api_down                 api_bg로 시작한 서버 종료"
        Write-Host "  update                   E1→E2→E3 원클릭 파이프라인"
        Write-Host ""
        Write-Host "Options:"
        Write-Host "  -Overlap rebuild|new_only"
        Write-Host "  -StartDate YYYY-MM-DD"
        Write-Host "  -EndDate   YYYY-MM-DD"
        Write-Host "  -DryRun"
        Write-Host ""
        Write-Host "대시보드: .\run.ps1 api_bg  →  http://localhost:8000/dashboard (터미널을 닫아도 유지)"
    }
}
