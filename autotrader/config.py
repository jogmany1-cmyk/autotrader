"""시스템 전체 파라미터를 한곳에 모은 설정 객체.

숫자 하나 바꿔서 스킴 전체를 재현하려면 값들이 코드에 흩어져 있으면 안 된다.
YAML 없이도 동작하고 (표준 라이브러리 yaml 있으면 로드) 필요할 때만 파일에서 읽는다.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any, Dict, List, Optional


@dataclass
class Costs:
    """실제 체결에 가까운 비용 모델. 백테스트가 현실을 과대평가하지 않게 하는 핵심.

    **이 dataclass 는 저장소에서 가장 파급력이 큰 몇 줄이다.** 여기가 틀리면
    이후 어떤 전략을 시험하든 채점판이 거짓말을 한다. 실제로 두 곳이 틀려
    있었다:

    1. `tax_sell_bp = 18.0` — 2026-01-01 부터 코스피·코스닥 **모두 0.20%**
       (코스피 = 증권거래세 0.05% + 농어촌특별세 0.15%, 코스닥 = 0.20%).
    2. `slippage_bp = 5.0` 고정 — 백테스터는 **다음 봉 시가에 체결**하므로
       시장가 주문이고, 시장가는 스프레드를 넘어가야 한다. KRX 틱 하나가
       가격의 5~25bp 라 편도 5bp 는 "항상 중간가 체결"을 모델링한 값이며
       도달 불가능하다.

    측정된 결과: 어떤 전략의 거래당 순수 우위(비용 전)가 +8.0bp 였는데
    필요한 왕복비용은 33bp(정정 후) ~ 103bp(코스닥 중소형 시장가) 였다.
    """
    commission_bp: float = 1.5      # 왕복 아닌 편도 수수료 (bp = 0.01%)
    # 2026-01-01 시행. 코스피 0.05%+농특세 0.15%, 코스닥 0.20% — 둘 다 20bp.
    tax_sell_bp: float = 20.0
    # tick 모드에서는 **하한**으로만 쓰인다. fixed 모드에서는 이 값 자체가
    # 편도 슬리피지가 된다 (구버전 재현·회귀 비교용).
    slippage_bp: float = 5.0
    # 시장가 주문이 넘어가는 호가 틱 수(편도). 1.0 = 한 틱을 온전히 넘긴다.
    # 유동성 좋은 대형주는 이보다 낮을 수 있고, 호가가 2~5틱 벌어지는 코스닥
    # 중소형주는 이보다 높다. 유니버스가 넓으면 보수적으로 올려 잡아야 한다.
    slippage_ticks: float = 1.0
    slippage_mode: str = "tick"     # "tick" | "fixed"
    borrow_bp_annual: float = 0.0   # 신용/대주 이자 (기본 0)

    @classmethod
    def legacy_2025(cls) -> "Costs":
        """2026-08-26 정정 **이전**의 비용 모델. 재현·비교 전용.

        새 모델이 결과를 얼마나 바꾸는지 재려면 같은 실행을 두 번 돌려
        비교해야 하는데, 그때마다 설정을 손으로 고치면 비교 자체를 믿기
        어렵다. 옛 값을 코드에 고정해 둔다.

        **이 설정으로 나온 숫자는 낙관 편향이다.** 세율이 2bp 낮고,
        슬리피지가 가격대와 무관하게 편도 5bp 로 고정돼 있어 시장가 주문이
        스프레드를 넘는 비용을 반영하지 못한다.
        """
        return cls(commission_bp=1.5, tax_sell_bp=18.0,
                   slippage_bp=5.0, slippage_mode="fixed")

    def slippage_bp_at(self, price: float) -> float:
        """이 가격에서의 편도 슬리피지(bp).

        tick 모드에서 `slippage_bp` 는 하한이다 — 호가단위가 아주 촘촘한
        고가 대형주라도 체결 지연·부분체결 비용이 0 은 아니기 때문이다.
        """
        if self.slippage_mode == "fixed" or price <= 0:
            return self.slippage_bp
        from .market import krx_tick_bp
        return max(self.slippage_bp, krx_tick_bp(price) * self.slippage_ticks)


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
    # 회전율 폭주 방지 (v0.7): 자동매매 실패 사례의 가장 큰 킬러는 수수료·세금.
    # 하루 신규 진입 상한을 두어 "5초 만에 -2% 손절 → 재진입 → 반복" 폭주를 원천 차단.
    max_trades_per_day: int = 8
    # 최고점 매수 방지 (v0.7): 직전 봉이 이 값 이상 급등한 종목은 진입 금지.
    # "포착 즉시 시장가 매수 → 곧바로 하락 → 로스컷" 사고를 사전에 차단.
    # 0 = 필터 비활성.
    chase_filter_pct: float = 0.05


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
    # 5개 앙상블 전략에는 넣지 않는다(backtest._default_strategies 미포함).
    # walkforward --strategy 단독 실행에서만 쓰이며, Ensemble 이
    # getattr(weights, strat.name) 으로 가중치를 찾으므로 필드는 있어야 한다.
    swing_trend_v2_experimental: float = 1.25


@dataclass
class ExecutionCfg:
    entry_gap_from_close_bp: float = 30.0   # 다음 봉 시가가 종가에서 이 폭 이상 벌어지면 취소
    order_type: str = "MARKET"              # MARKET | LIMIT
    limit_offset_bp: float = 20.0           # LIMIT 시 종가 대비 오프셋
    max_holding_bars: int = 20              # 강제 청산까지 최대 보유 봉수
    # 트레일링 폭을 변동성에 맞춘다. 고정 %는 종목마다 다른 변동성을 무시해서,
    # 변동성이 큰 종목에서는 정상적인 흔들림에도 잘려나간다.
    #
    # 애초의 근거는 "진입 신호가 10일 지평선에서 유의(t=2.81)" 였는데, 그 t 는
    # 중첩 구간 탓에 부풀려진 값이었다 (보정 후 0.89 — PITFALLS #25).
    # **그 근거는 이제 없다.** 남은 근거는 하나뿐이다: 종전 고정 5% 는 측정된
    # 일일 ATR(종가의 3.35%)의 1.5배, 즉 이틀치 변동밖에 못 견뎠다. 보유
    # 기간을 며칠로 보든 그보다는 넓어야 한다. √10 ≈ 3.16 은 "열흘치" 라는
    # 잠정 선택이며, 표본이 쌓이면 다시 재야 한다.
    #
    # 0 이면 이 기능을 끄고 고정 trail_pct 를 쓴다.
    trail_atr_mult: float = 10 ** 0.5
    trail_atr_period: int = 14
    # 데이트레이딩 규율 (v0.8): 이 시각(HH:MM, KST)이 되면 보유 전량을 일괄 청산.
    # None 이면 비활성. 밤 사이 갭·이벤트 리스크를 회피하는 정석적 방어.
    flat_at_time: str | None = None


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

# ---------------------------------------------------------------- 키움 프로토콜
# 아래 상수·함수는 키움 REST API 공식 문서에서 확인한 값이다. 추측이 아니다.
# 두 어댑터(data/kiwoom.py · broker/kiwoom.py)가 같은 규칙을 쓰도록 한곳에 둔다.

# 유량 제한 — 공식 문서 "API 호출 횟수 제한"
#   실서버 국내주식 조회 TR : 1초당 5회   → 0.2초 간격 (+25% 여유)
#   모의투자              : TR 1개당 1초 1회 → 1.0초 간격 (+10% 여유)
KIWOOM_INTERVAL_REAL = 0.25
KIWOOM_INTERVAL_PAPER = 1.1

# 토큰 유효기간 상한 — 이보다 큰 값이 나오면 파싱이 잘못된 것으로 본다.
_MAX_TOKEN_TTL = 7 * 24 * 3600.0
_TOKEN_EXPIRY_FORMATS = ("%Y%m%d%H%M%S", "%Y-%m-%d %H:%M:%S", "%Y%m%d%H%M", "%Y%m%d")


def kiwoom_min_interval(is_paper: bool) -> float:
    """모드별 최소 요청 간격(초)."""
    return KIWOOM_INTERVAL_PAPER if is_paper else KIWOOM_INTERVAL_REAL


def kiwoom_token_ttl(payload: Dict[str, Any], now: Optional[datetime] = None,
                     default: float = 43200.0) -> float:
    """토큰 응답에서 남은 유효 시간(초)을 뽑는다.

    공식 문서상 응답 필드는 `token` · `token_type` · **`expires_dt`(만료일)** 이다.
    `expires_in`(남은 초) 같은 필드는 없다. 그런데 이전 코드는 둘을 구분하지 않고
    이렇게 썼다.

        ttl = int(js.get("expires_in") or js.get("expires_dt", 43200))

    `expires_dt` 가 "20260825154600" 이면 int() 결과는 20조 초 — 약 64만 년이다.
    그 값을 만료 시각으로 삼으니 토큰이 영원히 유효하다고 착각하고, 서버에서
    실제로 만료된 뒤에도 재발급하지 않는다. 명령이 몇 초 만에 끝나는 CLI 에서는
    드러나지 않지만, 크론으로 하루 종일 도는 LiveTrader 는 인증 실패로 멈춘다.

    만료일은 한국시간으로 본다 (이 시스템의 시계가 KST 하나로 통일돼 있다).
    파싱이 실패하거나 값이 상식 밖이면 보수적으로 `default` 를 쓴다 — 조금 일찍
    재발급하는 것은 안전하지만, 늦게 재발급하는 것은 매매를 멈춘다.
    """
    from .market import now_kst          # 순환 임포트 방지를 위해 지역 임포트

    raw_in = payload.get("expires_in")
    if raw_in not in (None, ""):
        try:
            ttl = float(raw_in)
            if 0 < ttl <= _MAX_TOKEN_TTL:
                return ttl
        except (TypeError, ValueError):
            pass

    raw_dt = payload.get("expires_dt")
    if raw_dt not in (None, ""):
        text = str(raw_dt).strip()
        for fmt in _TOKEN_EXPIRY_FORMATS:
            try:
                expiry = datetime.strptime(text, fmt)
            except ValueError:
                continue
            # strptime 은 %m·%H·%M·%S 에서 1~2자리를 모두 허용한다. 그래서
            # 12자리 문자열이 14자리 형식으로 조용히 잘못 파싱된다:
            #   "202608251546" + "%Y%m%d%H%M%S" → 15:04:06  (15:46 이 아니다)
            # 그럴듯한 값이 나와 눈치채기 어렵다. 되돌려 찍어 원문과 같을 때만
            # 받아들인다.
            if expiry.strftime(fmt) != text:
                continue
            ttl = (expiry - (now or now_kst())).total_seconds()
            return ttl if 0 < ttl <= _MAX_TOKEN_TTL else default
    return default
