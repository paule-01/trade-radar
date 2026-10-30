"""eToro: neue und geschlossene Positionen von Popular Investors erkennen.

Nutzt die offizielle öffentliche eToro-API (public-api.etoro.com).
Benötigt die Secrets ETORO_API_KEY und ETORO_USER_KEY (nur Leserechte nötig).
"""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone

from .common import env, esc, http_error, fmt_pct, get, load_state, log, save_state, send_telegram, session, to_float
from .risk import ASSET_LABEL, TraderProfile, ampel, score_trade, score_trader, trader_facts

BASE = "https://public-api.etoro.com"
TYPE_MAP = {1: "currency", 2: "commodity", 4: "index", 5: "stock", 6: "etf", 10: "crypto"}
PROFILE_MAX_AGE_H = 24        # Trader-Kennzahlen 1x täglich auffrischen
DISCOVERY_MAX_AGE_H = 24 * 7  # Rangliste 1x pro Woche neu laden


# tradeinfo liefert Prozent (44.71 = 44,71 %), die Rangliste dagegen "gain" als Bruch (0.86).
# Ein Raten anhand der Größe (<= 1 -> Bruch) würde 0,5 % Hebelanteil zu 50 % machen.
_pct = to_float

# eToro setzt bei "kein Stop-Loss" Platzhalter (Kauf: 0.0001, Short: sehr hoher Kurs).
# Als echter Stop-Loss zählt nur einer, der höchstens so weit vom Einstieg entfernt liegt.
STOP_MAX_DISTANCE = 0.5


def has_stop_loss(sl: float | None, open_rate: float | None, short: bool) -> bool:
    if not sl or sl <= 0:
        return False
    if not open_rate:
        return sl > 0.001
    dist = (sl - open_rate) / open_rate if short else (open_rate - sl) / open_rate
    return dist <= STOP_MAX_DISTANCE


class EToro:
    def __init__(self):
        self.s = session({"accept": "application/json"})
        self.s.headers["x-api-key"] = env("ETORO_API_KEY")
        self.s.headers["x-user-key"] = env("ETORO_USER_KEY")

    def _get(self, path: str, **params):
        r = get(self.s, BASE + path, params=params, headers={"x-request-id": str(uuid.uuid4())})
        if r.status_code == 404:
            return None
        if not r.ok:
            log.error("eToro %s -> %s", path, http_error(r))
        r.raise_for_status()
        return r.json()

    def rankings(self, period: str, count: int, risk_max: int, min_copiers: int) -> list[dict]:
        data = self._get("/api/v2/portfolios/rankings", period=period, sort="-gain",
                         page=1, pageSize=count, popularInvestor="true",
                         riskScoreMax=str(risk_max), copiersMin=str(min_copiers))
        return (data or {}).get("results", [])

    def live_portfolio(self, user: str) -> dict | None:
        return self._get(f"/api/v1/user-info/people/{user}/portfolio/live")

    def trade_info(self, user: str, period: str = "OneYearAgo") -> dict | None:
        return self._get(f"/api/v1/user-info/people/{user}/tradeinfo", period=period)

    def instruments(self, ids: list[int]) -> dict[int, dict]:
        out: dict[int, dict] = {}
        for i in range(0, len(ids), 50):
            chunk = ids[i:i + 50]
            data = self._get("/api/v1/market-data/instruments",
                             instrumentIds=",".join(map(str, chunk))) or {}
            for d in data.get("instrumentDisplayDatas", []):
                out[int(d["instrumentID"])] = {
                    "name": d.get("instrumentDisplayName") or d.get("symbolFull"),
                    "symbol": d.get("symbolFull") or "",
                    "type": TYPE_MAP.get(d.get("instrumentTypeID"), "unknown"),
                }
        return out


def profile_from_tradeinfo(ti: dict, gain_fraction: float | None = None) -> TraderProfile:
    trades = to_float(ti.get("trades"))
    hi = _pct(ti.get("highLeveragePct"))
    weeks = to_float(ti.get("weeksSinceRegistration"))
    dd = _pct(ti.get("peakToValley"))
    gain = gain_fraction * 100 if gain_fraction is not None else _pct(ti.get("gain"))
    return TraderProfile(
        platform_risk=to_float(ti.get("riskScore")),
        platform_risk_max=to_float(ti.get("maxMonthlyRiskScore")),
        max_drawdown_pct=abs(dd) if dd is not None else None,
        trades_per_month=trades / 12 if trades is not None else None,
        high_leverage_pct=hi,
        win_ratio_pct=_pct(ti.get("winRatio")),
        profitable_months_pct=_pct(ti.get("profitableMonthsPct")),
        years_active=weeks / 52 if weeks is not None else None,
        gain_1y_pct=gain,
        copiers=int(ti["copiers"]) if ti.get("copiers") is not None else None,
    )


def _now() -> float:
    return time.time()


def run(cfg: dict, risk_cfg: dict) -> None:
    if not cfg.get("enabled", True):
        return
    if not env("ETORO_API_KEY"):
        log.warning("eToro übersprungen: ETORO_API_KEY fehlt")
        return
    api = EToro()
    st = load_state("etoro")
    st.setdefault("portfolios", {})
    st.setdefault("profiles", {})
    st.setdefault("instruments", {})

    # ---- Liste der Trader bestimmen
    users = {u.lower() for u in (cfg.get("traders") or [])}
    ad = cfg.get("auto_discover") or {}
    if ad.get("enabled"):
        disc = st.get("discovery", {})
        if _now() - disc.get("ts", 0) > DISCOVERY_MAX_AGE_H * 3600:
            try:
                rows = api.rankings(ad.get("period", "OneYearAgo"), ad.get("count", 15),
                                    ad.get("risk_score_max", 6), ad.get("min_copiers", 500))
                disc = {"ts": _now(), "users": [r["username"].lower() for r in rows],
                        "gain": {r["username"].lower(): r.get("gain") for r in rows}}
                st["discovery"] = disc
                log.info("eToro-Rangliste: %s", disc["users"])
            except Exception as e:
                log.error("eToro-Rangliste fehlgeschlagen: %s", e)
        users |= set(disc.get("users", []))
    gains = (st.get("discovery") or {}).get("gain", {})

    min_pct = float(cfg.get("min_position_pct", 0.5))
    messages: list[str] = []

    for user in sorted(users):
        # ---- Profil (1x täglich)
        prof_entry = st["profiles"].get(user)
        if not prof_entry or _now() - prof_entry.get("ts", 0) > PROFILE_MAX_AGE_H * 3600:
            try:
                ti = api.trade_info(user)
                if ti:
                    prof_entry = {"ts": _now(), "data": ti}
                    st["profiles"][user] = prof_entry
            except Exception as e:
                log.error("tradeinfo %s: %s", user, e)
        ti = (prof_entry or {}).get("data", {})
        profile = score_trader(profile_from_tradeinfo(ti, gains.get(user)), risk_cfg)

        # ---- Live-Depot
        try:
            pf = api.live_portfolio(user)
        except Exception as e:
            log.error("Portfolio %s: %s", user, e)
            continue
        if pf is None:
            log.warning("eToro-User %s nicht gefunden/privat", user)
            continue
        current = {str(p["positionId"]): p for p in pf.get("positions", [])}
        mirrors = {str(m["socialTradeId"]): m for m in pf.get("socialTrades", [])}

        prev = st["portfolios"].get(user)
        st["portfolios"][user] = {
            "positions": {k: _slim(v) for k, v in current.items()},
            "mirrors": {k: {"parentUsername": v.get("parentUsername"),
                            "investmentPct": v.get("investmentPct")} for k, v in mirrors.items()},
        }
        if prev is None:  # erster Lauf: nur merken, nichts melden
            log.info("eToro %s: Ausgangszustand gespeichert (%d Positionen)", user, len(current))
            continue

        prev_pos = prev.get("positions", {})
        opened = [current[k] for k in current if k not in prev_pos]
        closed = [prev_pos[k] for k in prev_pos if k not in current]
        held_instr = {p["instrumentId"] for k, p in prev_pos.items() if k in current}

        need = {p["instrumentId"] for p in opened + closed} - {int(i) for i in st["instruments"]}
        if need:
            try:
                for iid, meta in api.instruments(sorted(need)).items():
                    st["instruments"][str(iid)] = meta
            except Exception as e:
                log.error("Instrumente: %s", e)

        for p in opened:
            if (to_float(p.get("investmentPct")) or 0) < min_pct:
                continue
            add = p["instrumentId"] in held_instr
            messages.append(_msg(user, ti, profile, p, st["instruments"], "add" if add else "open", risk_cfg))
        if cfg.get("notify_closes", True):
            for p in closed:
                if (to_float(p.get("investmentPct")) or 0) < min_pct:
                    continue
                messages.append(_msg(user, ti, profile, p, st["instruments"], "close", risk_cfg))

        prev_m = prev.get("mirrors", {})
        for k, m in mirrors.items():
            if k not in prev_m:
                messages.append(
                    f"🔁 <b>{esc(user)}</b> kopiert jetzt <b>{esc(m.get('parentUsername'))}</b> "
                    f"({fmt_pct(to_float(m.get('investmentPct')))} des Depots)\n"
                    f"https://www.etoro.com/people/{esc(user)}/portfolio")
        time.sleep(1.1)  # Rate-Limit 60/min schonen

    save_state("etoro", st)
    for m in messages:
        send_telegram(m)
    log.info("eToro: %d Meldungen", len(messages))


def _slim(p: dict) -> dict:
    keys = ("positionId", "instrumentId", "isBuy", "leverage", "investmentPct", "openRate",
            "netProfit", "stopLossRate", "openTimestamp")
    return {k: p.get(k) for k in keys}


def _msg(user: str, ti: dict, profile: TraderProfile, p: dict, instr: dict,
         kind: str, risk_cfg: dict) -> str:
    meta = instr.get(str(p["instrumentId"]), {})
    name = meta.get("name") or f"Instrument #{p['instrumentId']}"
    sym = meta.get("symbol", "")
    asset = meta.get("type", "unknown")
    lev = to_float(p.get("leverage")) or 1
    pct = to_float(p.get("investmentPct"))
    short = p.get("isBuy") is False
    sl = to_float(p.get("stopLossRate"))
    has_stop = has_stop_loss(sl, to_float(p.get("openRate")), short)

    tr = score_trade(asset, lev, short, pct, has_stop)
    dot, label, total = ampel(tr, profile, risk_cfg)

    if kind == "close":
        action = "🔻 VERKAUFT / GESCHLOSSEN"
        np_ = to_float(p.get("netProfit"))
        extra = f"Ergebnis zuletzt: {fmt_pct(np_, sign=True)}" if np_ is not None else ""
    else:
        action = ("➕ NACHGEKAUFT" if kind == "add" else "🆕 NEU GEKAUFT") if not short else "📉 SHORT ERÖFFNET"
        extra = ""

    rate = to_float(p.get("openRate"))
    details = [("Einstieg " if kind == "close" else "Kurs ") + (f"{rate:g}".replace(".", ",") if rate else "?")]
    details.append(f"Hebel x{lev:g}" if lev > 1 else "ohne Hebel")
    if pct is not None:
        details.append(f"{fmt_pct(pct)} des Depots")
    if has_stop:
        details.append(f"Stop-Loss {sl:g}".replace(".", ","))
    full = ti.get("fullName")
    who = f"<b>{esc(user)}</b>" + (f" ({esc(full)})" if full else "")
    lines = [
        f"{dot} <b>{label}</b> · eToro · Gesamtrisiko {total:.0f}/100",
        f"👤 {who} – Profil: <b>{profile.label}</b>" + (f", {profile.activity}" if profile.activity else ""),
        f"{action}: <b>{esc(name)}</b>" + (f" ({esc(sym)})" if sym and sym != name else "")
        + f" · {ASSET_LABEL.get(asset, 'Wertpapier')}",
        " · ".join(details),
    ]
    if extra:
        lines.append(extra)
    lines.append(f"Trade-Risiko {tr.score:.0f}/100: {esc(', '.join(tr.reasons))}")
    facts = trader_facts(profile)
    if facts:
        lines.append(f"Trader: {esc(facts)}")
    ts = p.get("openTimestamp")
    if ts and kind != "close":
        try:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(timezone.utc)
            lines.append(f"Eröffnet: {dt:%d.%m. %H:%M} UTC")
        except ValueError:
            pass
    lines.append(f"https://www.etoro.com/people/{esc(user)}/portfolio")
    return "\n".join(lines)
