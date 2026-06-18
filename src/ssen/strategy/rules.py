"""
E3: 전략 룰 파라미터 정의.

체결 원칙 (룩어헤드 방지):
  - 신호 생성: t일 종가 기준 데이터로 계산
  - 진입/청산: t+1 거래일 close_price 체결
  - 동일 파라미터 + 동일 데이터 → 동일 결과 보장
"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
import hashlib, json


@dataclass
class RuleParams:
    # ── 주도주 선정 ─────────────────────────────────────────────────────────
    top_n: int = 50
    # 주도 테마: 해당 날 theme1_amount 최대인 테마
    # 주도주: 해당 테마 내 global rank 최소 종목

    # ── 레짐 정의 ──────────────────────────────────────────────────────────
    min_regime_days: int = 1          # 최소 레짐 지속 거래일 (미달 시 무시)

    # ── 갈아타기 조건 ─────────────────────────────────────────────────────
    switch_threshold_pct: float = 0.0
    # 신규 테마 거래대금 / 현재 테마 거래대금 - 1 >= switch_threshold_pct/100 일 때만 교체
    # 0.0 = 항상 교체 (테마가 바뀌면 즉시 신호)

    # ── 리스크 관리 ───────────────────────────────────────────────────────
    stop_loss_pct: float = 0.0        # 손절 % (0 = 비활성)
    take_profit_pct: float = 0.0      # 익절 % (0 = 비활성)

    # ── 비용 ─────────────────────────────────────────────────────────────
    fee_pct: float = 0.0              # 왕복 수수료 + 슬리피지 (기본 0)

    # ── 증분 재계산 ───────────────────────────────────────────────────────
    lookback_days: int = 30           # 레짐 경계 재계산 버퍼 (거래일)

    # ── 버전 ─────────────────────────────────────────────────────────────
    rule_version: str = "v1.0"

    def param_hash(self) -> str:
        """파라미터 딕셔너리의 MD5 앞 8자 (재현성 식별자)."""
        d = {k: v for k, v in asdict(self).items() if k != "rule_version"}
        return hashlib.md5(json.dumps(d, sort_keys=True).encode()).hexdigest()[:8]

    def to_dict(self) -> dict:
        return asdict(self)


# 기본 파라미터
DEFAULT_PARAMS = RuleParams()

# 보수적 파라미터 (3일 이상 지속, 손절 10%)
CONSERVATIVE_PARAMS = RuleParams(
    min_regime_days=3,
    stop_loss_pct=10.0,
    fee_pct=0.3,
    rule_version="v1.0-conservative",
)
