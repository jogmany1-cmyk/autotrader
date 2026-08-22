"""시스템 전체 파라미터를 한곳에 모은 설정 객체.

숫자 하나 바꿔서 스킴 전체를 재현하려면 값들이 코드에 흩어져 있으면 안 된다.
YAML 없이도 동작하고 (표준 라이브러리 yaml 있으면 로드) 필요할 때만 파일에서 읽는다.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional


@dataclass
class Costs:
    """실제 체결에 가까운 비용 모델. 백테스트가 현실을 과대평가하지 않게 하는 핵심."""
    commission_bp: float = 1.5      # 왕복 아닌 편도 수수료 (bp = 0.01%)
    tax_sell_bp: float = 18.0       # 매도 시 증권거래세+농특세 근사 (KOSPI 기준)
    slippage_bp: float = 5.0        # 유동성/체결 지연을 감안한 편도 슬리피지
    borrow_bp_annual: float = 0.0   # 신용/대주 이자 (기본 0)


@dataclass
class RiskLimits:
    max_position_pct: float = 0.20     # 종목당 자기자본 최대 비중
    max_positions: int = 5             # 동시 보유 종목 수 상한
    per_trade_risk_pct: float = 0.01   # 진입 1건이 감수할 최대 손실 = 자본의 1%
    daily_loss_stop_pct: float = 0.03  # 일일 손실이 3% 넘으면 그 날은 신규 진입 금지
    max_gross_exposure: float = 1.00   # 총 매수금액 / 자본 상한
    max_consecutive_losses: int = 5    # 연속 손절 N회 이후 하루 쿨다운
    min_cash_pct: float = 0.10         # 항상 남겨두는 현금 비율
    hard_stop_loss_pct: float = 0.10   # 개별 전략과 무관한 계좌 보호용 최종 손절 (-10%)
    cooldown_bars_after_stop: int = 3  # 손절/AI매도 후 재매수 금지 기간(거래일). 익절은 예외.
    ensemble_sell_threshold: float = 0.6   # AI SELL 신호 채택 임계값 (신뢰도)


@dataclass
class Universe:
    symbols: List[str] = field(default_factory=list)
    min_price: float = 1_000.0        # 지나치게 저가인 종목 제외
    min_avg_dollar_vol: float = 5e8   # 하루 평균 거래대금 하한(합성값 기준)
    lookback_days: int = 250          # 전략·팩터 계산에 쓸 과거 봉 수


@dataclass
class SymbolProfile:
    """ETF 와 개별주는 위험 프로파일이 크게 달라서 손절폭·트레일링·앙상블
    임계값을 따로 잡는다. 블로그 후기 개선판 ⑥·⑧과 같은 취지."""
    trail_pct: float = 0.05
    max_holding_bars: int = 20
    ensemble_threshold: float = 0.55
    hard_stop_loss_pct: float = 0.10


@dataclass
class SymbolProfiles:
    etf: SymbolProfile = field(default_factory=lambda: SymbolProfile(
        trail_pct=0.04, max_holding_bars=40,
        ensemble_threshold=0.50, hard_stop_loss_pct=0.07,
    ))
    stock: SymbolProfile = field(default_factory=lambda: SymbolProfile(
        trail_pct=0.06, max_holding_bars=15,
        ensemble_threshold=0.58, hard_stop_loss_pct=0.10,
    ))

    def for_kind(self, kind: str) -> SymbolProfile:
        kind = (kind or "stock").lower()
        return self.etf if kind == "etf" else self.stock


@dataclass
class StrategyWeights:
    """앙상블 가중치. 어떤 전략이 얼마나 목소리를 낼지."""
    day_breakout: float = 1.0
    day_pullback: float = 1.0
    day_momentum: float = 0.75
    swing_trend: float = 1.25
    mean_reversion: float = 0.75


@dataclass
class ExecutionCfg:
    entry_gap_from_close_bp: float = 30.0   # 다음 봉 시가가 종가에서 이 폭 이상 벌어지면 취소
    order_type: str = "MARKET"              # MARKET | LIMIT
    limit_offset_bp: float = 20.0           # LIMIT 시 종가 대비 오프셋
    max_holding_bars: int = 20              # 강제 청산까지 최대 보유 봉수


@dataclass
class BacktestCfg:
    initial_cash: float = 10_000_000.0
    train_ratio: float = 0.6
    val_ratio: float = 0.2
    # 나머지는 out-of-sample. 세 구간 합이 1이 되게 강제.

    def splits(self, n: int) -> Dict[str, slice]:
        train_end = int(n * self.train_ratio)
        val_end = int(n * (self.train_ratio + self.val_ratio))
        return {
            "train": slice(0, train_end),
            "val": slice(train_end, val_end),
            "oos": slice(val_end, n),
        }


@dataclass
class KISConfig:
    """한국투자증권 Open API. 값이 채워지지 않으면 KIS 어댑터는 안전하게 비활성."""
    app_key: str = ""
    app_secret: str = ""
    account_number: str = ""
    account_product_code: str = "01"
    is_paper: bool = True

    @classmethod
    def from_env(cls) -> "KISConfig":
        return cls(
            app_key=os.getenv("KIS_APP_KEY", ""),
            app_secret=os.getenv("KIS_APP_SECRET", ""),
            account_number=os.getenv("KIS_ACCOUNT_NUMBER", ""),
            account_product_code=os.getenv("KIS_ACCOUNT_PRODUCT_CODE", "01"),
            is_paper=os.getenv("KIS_MODE", "paper").lower() != "real",
        )


@dataclass
class KiwoomConfig:
    """키움증권 Open API (REST). v0.5 KiwoomConditionStream(WebSocket) 과 짝.
    앱키·시크릿은 개발자 센터에서 실전용/모의투자용을 따로 발급받는다."""
    app_key: str = ""
    app_secret: str = ""
    account_number: str = ""
    is_paper: bool = True

    @classmethod
    def from_env(cls) -> "KiwoomConfig":
        return cls(
            app_key=os.getenv("KIWOOM_APP_KEY", ""),
            app_secret=os.getenv("KIWOOM_APP_SECRET", ""),
            account_number=os.getenv("KIWOOM_ACCOUNT_NUMBER", ""),
            is_paper=os.getenv("KIWOOM_MODE", "paper").lower() != "real",
        )


@dataclass
class Config:
    costs: Costs = field(default_factory=Costs)
    risk: RiskLimits = field(default_factory=RiskLimits)
    universe: Universe = field(default_factory=Universe)
    weights: StrategyWeights = field(default_factory=StrategyWeights)
    execution: ExecutionCfg = field(default_factory=ExecutionCfg)
    backtest: BacktestCfg = field(default_factory=BacktestCfg)
    kis: KISConfig = field(default_factory=KISConfig)
    kiwoom: KiwoomConfig = field(default_factory=KiwoomConfig)
    profiles: SymbolProfiles = field(default_factory=SymbolProfiles)
    symbol_kinds: Dict[str, str] = field(default_factory=dict)  # symbol → "etf"|"stock"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def default(cls) -> "Config":
        return cls()

    @classmethod
    def load(cls, path: Optional[str] = None) -> "Config":
        cfg = cls.default()
        if not path or not os.path.exists(path):
            return cfg
        try:
            import yaml  # type: ignore
        except Exception:
            return cfg
        with open(path, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
        return _merge(cfg, raw)


def _merge(cfg: Config, raw: Dict[str, Any]) -> Config:
    """평범한 dict 를 dataclass 트리에 얹는다 (부분 갱신 허용)."""
    def _apply(obj, values):
        if not isinstance(values, dict):
            return
        for k, v in values.items():
            if hasattr(obj, k):
                cur = getattr(obj, k)
                if hasattr(cur, "__dataclass_fields__") and isinstance(v, dict):
                    _apply(cur, v)
                else:
                    setattr(obj, k, v)
    _apply(cfg, raw)
    return cfg
