"""Risiko-Einordnung für Trader (Profil) und einzelne Trades.

Alle Werte werden auf 0-100 Punkte normiert (0 = sehr vorsichtig, 100 = sehr riskant).
Die Ampel ergibt sich aus 60 % Trade-Risiko + 40 % Trader-Profil.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .common import fmt_pct

# Grund-Risiko je Anlageklasse (Punkte)
ASSET_BASE = {
    "etf": 10,
    "fund": 10,
    "index": 25,
    "bond": 10,
    "stock": 35,
    "commodity": 45,
    "currency": 55,
    "crypto": 75,
    "leveraged": 80,   # Hebelprodukte, Knock-outs, Optionsscheine
    "unknown": 40,
}
ASSET_LABEL = {
    "etf": "ETF", "fund": "Fonds", "index": "Index", "bond": "Anleihe", "stock": "Aktie",
    "commodity": "Rohstoff", "currency": "Devisen", "crypto": "Krypto",
    "leveraged": "Hebelprodukt", "unknown": "Wertpapier",
}


@dataclass
class TraderProfile:
    """Kennzahlen eines Traders – alles optional, fehlende Werte werden ignoriert."""
    platform_risk: float | None = None        # eToro 1-10
    platform_risk_max: float | None = None    # höchster Monats-Risikowert
    max_drawdown_pct: float | None = None     # positiv, z.B. 32 = -32 %
    volatility_pct: float | None = None       # annualisiert (wikifolio)
    trades_per_month: float | None = None
    high_leverage_pct: float | None = None
    win_ratio_pct: float | None = None
    profitable_months_pct: float | None = None
    years_active: float | None = None
    gain_1y_pct: float | None = None
    copiers: int | None = None
    score: float = 50
    label: str = "ausgewogen"
    activity: str = ""
    reasons: list[str] = field(default_factory=list)


def _lin(v, lo, hi):
    """Lineare Abbildung von v in [lo, hi] auf 0..100."""
    if hi == lo:
        return 0
    return max(0.0, min(100.0, (v - lo) / (hi - lo) * 100))


def score_trader(p: TraderProfile, cfg: dict | None = None) -> TraderProfile:
    cfg = cfg or {}
    yellow, red = cfg.get("yellow_from", 35), cfg.get("red_from", 65)
    parts: list[tuple[float, float]] = []  # (Punkte, Gewicht)
    if p.platform_risk is not None:
        parts.append((_lin(p.platform_risk, 1, 9), 35))
    if p.max_drawdown_pct is not None:
        parts.append((_lin(p.max_drawdown_pct, 5, 50), 25))
    elif p.volatility_pct is not None:
        parts.append((_lin(p.volatility_pct, 8, 45), 25))
    if p.high_leverage_pct is not None:
        parts.append((_lin(p.high_leverage_pct, 0, 40), 15))
    if p.trades_per_month is not None:
        parts.append((_lin(p.trades_per_month, 2, 80), 15))
    if p.years_active is not None:
        parts.append((_lin(-p.years_active, -4, -0.5), 10))  # kurze Historie = riskanter
    if parts:
        p.score = sum(v * w for v, w in parts) / sum(w for _, w in parts)
    p.label = "risikofreudig" if p.score >= red else "ausgewogen" if p.score >= yellow else "konservativ"

    if p.trades_per_month is None:
        p.activity = ""
    elif p.trades_per_month < 4:
        p.activity = "Langfrist-Anleger, handelt selten"
    elif p.trades_per_month < 20:
        p.activity = "regelmäßig aktiv"
    else:
        p.activity = "Vieltrader"
    return p


def trader_facts(p: TraderProfile) -> str:
    f = []
    if p.platform_risk is not None:
        s = f"eToro-Risiko {p.platform_risk:.0f}/10"
        if p.platform_risk_max and p.platform_risk_max > p.platform_risk:
            s += f" (max. {p.platform_risk_max:.0f})"
        f.append(s)
    if p.max_drawdown_pct is not None:
        f.append(f"max. Verlust {fmt_pct(p.max_drawdown_pct, 0)}")
    if p.volatility_pct is not None:
        f.append(f"Volatilität {fmt_pct(p.volatility_pct, 0)}")
    if p.trades_per_month is not None:
        f.append(f"{p.trades_per_month:.0f} Trades/Monat")
    if p.high_leverage_pct:
        f.append(f"{fmt_pct(p.high_leverage_pct, 0)} mit hohem Hebel")
    if p.win_ratio_pct is not None:
        f.append(f"Trefferquote {fmt_pct(p.win_ratio_pct, 0)}")
    if p.profitable_months_pct is not None:
        f.append(f"profitable Monate {fmt_pct(p.profitable_months_pct, 0)}")
    if p.gain_1y_pct is not None:
        f.append(f"Rendite 12 M. {fmt_pct(p.gain_1y_pct, 0, sign=True)}")
    if p.years_active is not None:
        f.append(f"aktiv seit {p.years_active:.1f}".replace(".", ",") + " J.")
    if p.copiers:
        f.append(f"{p.copiers:,} Kopierer".replace(",", "."))
    return " · ".join(f)


@dataclass
class TradeRisk:
    score: float
    reasons: list[str]


def score_trade(asset: str, leverage: float = 1, is_short: bool = False,
                position_pct: float | None = None, has_stop: bool | None = None) -> TradeRisk:
    asset = asset if asset in ASSET_BASE else "unknown"
    s = ASSET_BASE[asset]
    r: list[str] = []
    if asset in ("crypto", "leveraged", "currency"):
        r.append(ASSET_LABEL[asset])
    if leverage and leverage > 1:
        s += 15 if leverage < 5 else 35 if leverage < 10 else 50
        r.append(f"Hebel x{leverage:g}")
    if is_short:
        s += 20
        r.append("Leerverkauf (Wette auf fallende Kurse)")
    if position_pct is not None:
        if position_pct >= 20:
            s += 20
            r.append(f"sehr große Position ({fmt_pct(position_pct)})")
        elif position_pct >= 10:
            s += 10
            r.append(f"große Position ({fmt_pct(position_pct)})")
    if leverage and leverage > 1 and has_stop is False:
        s += 5
        r.append("kein Stop-Loss")
    if not r:
        r.append(f"{ASSET_LABEL[asset]} ohne Hebel")
    return TradeRisk(min(100, s), r)


def ampel(trade: TradeRisk, trader: TraderProfile, cfg: dict | None = None) -> tuple[str, str, float]:
    cfg = cfg or {}
    yellow, red = cfg.get("yellow_from", 35), cfg.get("red_from", 65)
    total = 0.6 * trade.score + 0.4 * trader.score
    # Ein sehr riskanter Trade (Hebelprodukt, Krypto mit Hebel …) bleibt riskant,
    # auch wenn der Trader sonst vorsichtig ist.
    if trade.score >= 80:
        total = max(total, red)
    if total >= red:
        return "🔴", "RISKANT", total
    if total >= yellow:
        return "🟡", "MITTEL", total
    return "🟢", "KONSERVATIV", total
