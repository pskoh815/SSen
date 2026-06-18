# Strategy Contract (E3)

## 룩어헤드 방지 원칙

**체결 기준 (변경 불가):**

```
t일   → 관측: theme1_amount, rank, close_price 등 당일 데이터로 신호 계산
t+1일 → 체결: 다음 거래일 close_price로 진입 또는 청산
```

- t일 데이터로 계산한 결과를 t일 안에 사용하지 않음
- t+1일 가격은 t일 신호 계산에 절대 참조하지 않음
- 손절/익절: t일 close 확인 → t+1일 close 체결 (동일 원칙 적용)

## 주도 테마 / 주도주 정의

| 용어 | 정의 |
|------|------|
| 주도 테마 | 해당 날 `theme1_amount`(테마 전체 거래대금) 최대인 `theme1` |
| 주도주 | 주도 테마에 속하는 종목 중 전체 `rank`가 가장 낮은(거래대금 최상위) 종목 |
| 레짐 | 동일 주도 테마가 연속으로 유지되는 거래일 구간 |
| 갈아타기 | 주도 테마 교체 시점, `switch_threshold_pct` 조건 충족 필요 |

## 파생 테이블

### derived_theme_daily
- 날짜 × 테마 조합별 집계
- `is_top_theme=TRUE` 행이 그 날의 주도 테마
- 재현 키: `(date, theme1, rule_version, dataset_version)`

### derived_leader_regime
- 주도 테마 연속 구간 (레짐)
- `start_date` = 레짐 첫 날, `end_date` = 레짐 마지막 날
- `leader_code` = 레짐 마지막 날 기준 주도주

### derived_trades
- 진입/청산 로그
- `signal_date` = 레짐 시작일 (t) — 신호 발생
- `entry_date` = t+1 거래일 — 진입 체결
- `exit_date` = 레짐 종료 다음 거래일 — 청산 체결
- `exit_reason`: `regime_end` | `stop_loss` | `take_profit` | `open`

## 룰 파라미터 (RuleParams)

| 파라미터 | 기본값 | 설명 |
|----------|--------|------|
| `top_n` | 50 | 상위 N 종목 내에서 주도주 선정 |
| `min_regime_days` | 1 | 최소 레짐 지속 거래일 (미달 시 거래 제외) |
| `switch_threshold_pct` | 0.0 | 갈아타기 임계치 % (0=항상 교체) |
| `stop_loss_pct` | 0.0 | 손절 % (0=비활성) |
| `take_profit_pct` | 0.0 | 익절 % (0=비활성) |
| `fee_pct` | 0.0 | 왕복 수수료 % |
| `lookback_days` | 30 | 증분 재계산 버퍼 (캘린더 일) |

## 증분 재계산

```
recalc_start = new_min_date - lookback_days (캘린더)
1. recalc_start 이후의 derived_* 삭제 (rule_version, dataset_version 기준)
2. recalc_start부터 재계산
3. 레짐 경계가 lookback 버퍼 내에서 올바르게 복원됨
```

## 재현성

- 동일 `rule_version` + 동일 `dataset_version` + 동일 Parquet → 동일 결과
- `dataset_version` = Parquet manifest의 `max_date`
- `param_hash()` 메서드로 파라미터 지문 생성 가능
