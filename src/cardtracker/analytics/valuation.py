"""Cross-sectional valuation: is a player's card cheap or expensive for how good they are?

Fit log(price) = a + b * performance_score across comparable players. The residual is
the signal: negative = cheap relative to performance, positive = priced on hype.
This is a starting point; more regressors (scarcity, age, market size) come later.
"""

import math
from dataclasses import dataclass


@dataclass
class Valuation:
    player_id: int
    price: float
    performance: float
    fair_price: float
    residual: float          # log(price) - log(fair_price)


def zscores(values: dict[int, float]) -> dict[int, float]:
    """Standardize a metric across a peer group."""
    xs = list(values.values())
    mean = sum(xs) / len(xs)
    sd = math.sqrt(sum((x - mean) ** 2 for x in xs) / len(xs)) or 1.0
    return {k: (v - mean) / sd for k, v in values.items()}


def fit_residuals(prices: dict[int, float], performance: dict[int, float]) -> list[Valuation]:
    """OLS of log(price) on performance over players present in both inputs.

    Returns valuations sorted most-undervalued first.
    """
    ids = [p for p in prices if p in performance and prices[p] > 0]
    if len(ids) < 3:
        raise ValueError("need at least 3 players to fit a peer regression")
    xs = [performance[p] for p in ids]
    ys = [math.log(prices[p]) for p in ids]
    x_mean, y_mean = sum(xs) / len(xs), sum(ys) / len(ys)
    sxx = sum((x - x_mean) ** 2 for x in xs)
    slope = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys)) / sxx if sxx else 0.0
    intercept = y_mean - slope * x_mean

    out = []
    for p, x, y in zip(ids, xs, ys):
        fitted = intercept + slope * x
        out.append(Valuation(p, prices[p], x, math.exp(fitted), y - fitted))
    return sorted(out, key=lambda v: v.residual)
