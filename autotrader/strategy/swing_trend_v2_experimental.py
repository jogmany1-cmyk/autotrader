"""SWING-01-v2(실험) · 추세추종, 변동성 우선순위.

`docs/WALKFORWARD-SPEC.md` §8 진단에서 확인된 원인에 대한 처방이다:
`exits.py` 의 `hard_stop` (계좌 공통 고정 -10%) 이 전략 자체 손절보다 항상
먼저 발동하는데(`EXIT_PRIORITY`), `swing_trend` 는 신호의 93.5%에서 자체
손절이 체결가보다 20% 이상 떨어져 있었다. 그 결과 변동성이 큰 종목은 방향과
무관하게 정상적인 가격 흔들림만으로 고정 -10% 에 먼저 잘려나간다. ATR/종가와
성적(hard_stop 비율·PF)이 정확히 단조 관계였던 것은 우연이 아니라 이 코드
경로 때문이다.

`swing_trend` 원본은 건드리지 않는다 — 별도 실험 전략으로 분리해 기존과
비교 가능하게 유지한다. 바뀐 것은 딱 하나, strength(=점수) 계산뿐이다:

- 진입 조건(50/200 정배열 · 종가>50이평 · 120봉 최소 모멘텀), 손절·목표가·
  최대 보유기간은 `swing_trend` 와 **완전히 동일**하다.
- 120봉 수익률이 클수록 점수를 올리던 것을 그만둔다 — 급등 종목일수록
  점수가 높아지지만 실제 성적은 좋아지지 않았고(§8), 급등 종목은 대개
  변동성도 같이 크다.
- 대신 변동성(ATR/종가)이 낮을수록 점수가 높다 — "1 ÷ 변동성"을 그대로
  쓰지 않고 `1 / (1 + ATR%)` 로 정규화한다. 원 역수는 ATR%→0 에서 발산하고
  스케일을 고정하려면 이번 데이터를 보고 정할 상수가 필요해진다.
  `1/(1+x)` 는 상수 없이 항상 (0, 1) 안에 들어오면서 ATR% 가 작을수록
  단조적으로 커지는, 이 데이터를 보기 전에도 고를 수 있는 형태다.

  실제 매매에 영향을 주는 것은 점수의 **상대적 순서**뿐이다. 포지션 자리가
  부족할 때 후보는 점수 내림차순으로 채워지므로(`backtest.py`), 결과적으로
  "변동성 낮은 종목 우선"이 된다. ATR% 4%, 10% 같은 경계값으로 종목을
  **탈락**시키지는 않는다 — 순위만 바꾼다.

주의(사전 공개): `score` 는 `RiskEngine.evaluate_entry` 의 포지션 크기
조절에도 쓰인다(`risk_budget ∝ clip(score*1.2, 0.5, 1.5)`). 이 전략의 점수
범위(대략 0.83~0.99, ATR% 1~20% 기준)에서는 그 계수가 1.0~1.19배로 거의
영향이 없지만, "선택 순위만 바꾼다"는 의도와 별개로 저변동성 종목의
포지션이 아주 조금 더 커지는 부수효과가 있다는 점을 남겨둔다.
"""
from __future__ import annotations

from .. import indicators as ind
from ..models import Bar, Side, Signal
from .base import Strategy, StrategyContext, StrategyResult


class SwingTrendV2Experimental(Strategy):
    name = "swing_trend_v2_experimental"

    def __init__(self, fast: int = 50, slow: int = 200, atr_p: int = 14,
                 min_roc_120: float = 0.05):
        self.fast = fast
        self.slow = slow
        self.atr_p = atr_p
        self.min_roc_120 = min_roc_120
        self.warmup = slow + 5

    def evaluate(self, ctx: StrategyContext) -> StrategyResult:
        gr = self._guard(ctx)
        if gr:
            return gr
        cur = ctx.bars[ctx.at]
        ma_f = ind.sma_at(ctx, self.fast)
        ma_s = ind.sma_at(ctx, self.slow)
        atr_val = ind.atr_at(ctx, self.atr_p)
        r120 = ind.roc_at(ctx, min(120, ctx.at))
        if None in (ma_f, ma_s, atr_val, r120) or atr_val <= 0:
            return StrategyResult.hold("nan")
        if not (ma_f > ma_s and cur.close > ma_f and r120 >= self.min_roc_120):
            return StrategyResult.hold("no-trend")
        atr_pct = atr_val / cur.close
        # 상수 없이 (0, 1) 안에서 ATR% 에 단조 감소. 결과를 보고 고른 값이
        # 아니다 — 스케일이 필요 없는 형태를 골랐다.
        strength = 1.0 / (1.0 + atr_pct)
        stop = min(cur.close - 2.5 * atr_val, ma_s)
        target = cur.close + 5.0 * atr_val
        return StrategyResult(
            Signal(Side.BUY, strength, f"trend(v2) r120 {r120*100:.1f}% "
                   f"atr {atr_pct*100:.1f}%"),
            stop_hint=stop, target_hint=target,
            factors={
                "roc_120": r120,
                "fast_slow_gap": ma_f / ma_s - 1.0,
                "price_fast_gap": cur.close / ma_f - 1.0,
                "atr_pct": atr_pct,
            },
        )
