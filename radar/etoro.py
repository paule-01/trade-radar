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
DIGEST_FROM = 4               # mehr geänderte Instrumente pro Lauf -> eine Sammelmeldung
LATE_AFTER_MIN = 30           # ältere Trades bekommen einen Verzögerungshinweis


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
        if not cfg.get("notify_closes", True):
            closed = []

        need = {p["instrumentId"] for p in opened + closed} - {int(i) for i in st["instruments"]}
        if need:
            try:
                for iid, meta in api.instruments(sorted(need)).items():
                    st["instruments"][str(iid)] = meta
            except Exception as e:
                log.error("Instrumente: %s", e)

        changes = group_changes(prev_pos, current, opened, closed, min_pct)
        if len(changes) > DIGEST_FROM:
            messages.append(_digest(user, ti, profile, changes, st["instruments"], risk_cfg))
        else:
            messages += [_msg(user, ti, profile, ch, st["instruments"], risk_cfg) for ch in changes]

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


# ---------------------------------------------------------------- Zusammenfassen
# Jeder Nachkauf ist bei eToro eine eigene positionId. Gemeldet wird deshalb pro Trader und
# Instrument nur eine Änderung je Lauf, egal wie viele Teilpositionen dahinterstecken.
def group_changes(prev_pos: dict, current: dict, opened: list[dict], closed: list[dict],
                  min_pct: float) -> list[dict]:
    held_before = {p["instrumentId"] for p in prev_pos.values()}
    new_ids = {str(p["positionId"]) for p in opened}
    groups: dict[int, dict] = {}
    for key, ps in (("opened", opened), ("closed", closed)):
        for p in ps:
            g = groups.setdefault(p["instrumentId"], {"iid": p["instrumentId"], "opened": [], "closed": []})
            g[key].append(p)
    out = []
    for iid, g in groups.items():
        # Schwelle gilt für die Summe der Teilpositionen
        if _sum_pct(g["opened"]) < min_pct and _sum_pct(g["closed"]) < min_pct:
            continue
        g["remaining"] = sum(1 for k, p in current.items() if p["instrumentId"] == iid and k not in new_ids)
        if g["opened"] and g["closed"]:
            g["kind"] = "swap"
        elif g["opened"]:
            g["kind"] = "add" if iid in held_before else "open"
        else:
            g["kind"] = "partial" if g["remaining"] else "close"
        out.append(g)
    return out


def _sum_pct(ps: list[dict]) -> float:
    return sum(to_float(p.get("investmentPct")) or 0 for p in ps)


def _avg_rate(ps: list[dict]) -> float | None:
    """Ø-Einstiegskurs gewichtet nach investiertem Anteil (= Betrag / Stückzahl)."""
    rows = [(to_float(p.get("investmentPct")) or 0, to_float(p.get("openRate"))) for p in ps]
    rows = [(w, r) for w, r in rows if r]
    if not rows:
        return None
    if sum(w for w, _ in rows) <= 0:
        return sum(r for _, r in rows) / len(rows)
    return sum(w for w, _ in rows) / sum(w / r for w, r in rows)


def _avg_result(ps: list[dict]) -> float | None:
    """Ø-Ergebnis in %, gewichtet nach investmentPct."""
    rows = [(to_float(p.get("investmentPct")) or 0, to_float(p.get("netProfit"))) for p in ps]
    rows = [(w, v) for w, v in rows if v is not None]
    if not rows:
        return None
    tw = sum(w for w, _ in rows)
    if tw <= 0:
        return sum(v for _, v in rows) / len(rows)
    return sum(w * v for w, v in rows) / tw


def _times(ps: list[dict]) -> list[datetime]:
    out = []
    for p in ps:
        ts = p.get("openTimestamp")
        if not ts:
            continue
        try:
            out.append(datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(timezone.utc))
        except ValueError:
            pass
    return sorted(out)


def _num(v: float | None) -> str:
    return f"{v:.6g}".replace(".", ",") if v else "?"


def _n(count: int) -> str:
    return f"{count} Position" + ("" if count == 1 else "en")


def _risk(ch: dict, instr: dict, risk_cfg: dict, profile: TraderProfile):
    asset = instr.get(str(ch["iid"]), {}).get("type", "unknown")
    ps = ch["opened"] or ch["closed"]  # Risiko der neuen Positionen, bei reinem Verkauf der alten
    lev = max((to_float(p.get("leverage")) or 1 for p in ps), default=1)
    short = any(p.get("isBuy") is False for p in ps)
    has_stop = all(has_stop_loss(to_float(p.get("stopLossRate")), to_float(p.get("openRate")),
                                 p.get("isBuy") is False) for p in ps)
    tr = score_trade(asset, lev, short, _sum_pct(ps), has_stop)
    return tr, ampel(tr, profile, risk_cfg), lev, short


def _title(ch: dict, instr: dict) -> str:
    meta = instr.get(str(ch["iid"]), {})
    name = meta.get("name") or f"Instrument #{ch['iid']}"
    sym = meta.get("symbol", "")
    return f"<b>{esc(name)}</b>" + (f" ({esc(sym)})" if sym and sym != name else "")


def _action(ch: dict, short: bool) -> str:
    kind = ch["kind"]
    if kind == "swap":
        return "🔄 UMGESCHICHTET"
    if kind == "close":
        return "🔻 VERKAUFT / GESCHLOSSEN"
    if kind == "partial":
        return "➖ TEILWEISE VERKAUFT"
    if short:
        return "📉 SHORT ERÖFFNET"
    return "➕ NACHGEKAUFT" if kind == "add" else "🆕 NEU GEKAUFT"


def _open_part(ps: list[dict], lev: float, count: bool = True) -> str:
    rate = _avg_rate(ps)
    parts = [("Kurs " if len(ps) == 1 else "Ø-Kurs ") + _num(rate)]
    parts.append(f"Hebel x{lev:g}" if lev > 1 else "ohne Hebel")
    parts.append(f"{fmt_pct(_sum_pct(ps))} des Depots")
    if count and len(ps) > 1:
        parts.append(_n(len(ps)))
    return " · ".join(parts)


def _close_part(ps: list[dict], count: bool = True) -> str:
    parts = ([_n(len(ps))] if count else []) + [f"{fmt_pct(_sum_pct(ps))} des Depots"]
    res = _avg_result(ps)
    if res is not None:
        parts.append(("Ergebnis " if len(ps) == 1 else "Ø-Ergebnis ") + fmt_pct(res, sign=True))
    return " · ".join(parts)


def _time_lines(ps: list[dict]) -> list[str]:
    ts = _times(ps)
    if not ts:
        return []
    a, b = ts[0], ts[-1]
    if a == b:
        span = f"{a:%d.%m. %H:%M}"
    elif a.date() == b.date():
        span = f"{a:%d.%m. %H:%M}–{b:%H:%M}"
    else:
        span = f"{a:%d.%m. %H:%M}–{b:%d.%m. %H:%M}"
    lines = [f"Eröffnet: {span} UTC"]
    late = (_now() - b.timestamp()) / 60
    if late > LATE_AFTER_MIN:
        lines.append(f"Gemeldet mit Verzögerung: ca. {late:.0f} Min.")
    return lines


def _who(user: str, ti: dict, profile: TraderProfile) -> str:
    full = ti.get("fullName")
    who = f"<b>{esc(user)}</b>" + (f" ({esc(full)})" if full else "")
    return f"👤 {who} – Profil: <b>{profile.label}</b>" + (f", {profile.activity}" if profile.activity else "")


def _msg(user: str, ti: dict, profile: TraderProfile, ch: dict, instr: dict, risk_cfg: dict) -> str:
    tr, (dot, label, total), lev, short = _risk(ch, instr, risk_cfg, profile)
    asset = instr.get(str(ch["iid"]), {}).get("type", "unknown")
    lines = [
        f"{dot} <b>{label}</b> · eToro · Gesamtrisiko {total:.0f}/100",
        _who(user, ti, profile),
        f"{_action(ch, short)}: {_title(ch, instr)} · {ASSET_LABEL.get(asset, 'Wertpapier')}",
    ]
    if ch["kind"] == "swap":
        lines.append(f"🔻 {_n(len(ch['closed']))} geschlossen: " + _close_part(ch["closed"], count=False))
        lines.append(f"🆕 {_n(len(ch['opened']))} neu eröffnet: " + _open_part(ch["opened"], lev, count=False))
    elif ch["opened"]:
        lines.append(_open_part(ch["opened"], lev))
        if len(ch["opened"]) == 1:
            p = ch["opened"][0]
            sl = to_float(p.get("stopLossRate"))
            if has_stop_loss(sl, to_float(p.get("openRate")), short):
                lines[-1] += f" · Stop-Loss {_num(sl)}"
    else:
        lines.append(_close_part(ch["closed"]))
        if ch["kind"] == "partial":
            lines.append(f"Noch im Depot: {_n(ch['remaining'])}")
    lines.append(f"Trade-Risiko {tr.score:.0f}/100: {esc(', '.join(tr.reasons))}")
    facts = trader_facts(profile)
    if facts:
        lines.append(f"Trader: {esc(facts)}")
    lines += _time_lines(ch["opened"])
    lines.append(f"https://www.etoro.com/people/{esc(user)}/portfolio")
    return "\n".join(lines)


def _digest(user: str, ti: dict, profile: TraderProfile, changes: list[dict], instr: dict,
            risk_cfg: dict) -> str:
    """Sammelmeldung: Kopf einmal (Ampel des riskantesten Trades), dann eine Zeile pro Instrument."""
    rows, worst = [], None
    for ch in changes:
        tr, (dot, label, total), lev, short = _risk(ch, instr, risk_cfg, profile)
        if worst is None or total > worst[2]:
            worst = (dot, label, total)
        if ch["kind"] == "swap":
            info = f"−{fmt_pct(_sum_pct(ch['closed']))} / +{fmt_pct(_sum_pct(ch['opened']))}"
        elif ch["opened"]:
            info = (f"{fmt_pct(_sum_pct(ch['opened']))} · Kurs {_num(_avg_rate(ch['opened']))}"
                    + (f" · Hebel x{lev:g}" if lev > 1 else ""))
        else:
            res = _avg_result(ch["closed"])
            info = fmt_pct(_sum_pct(ch["closed"])) + (f" · Ergebnis {fmt_pct(res, sign=True)}" if res is not None else "")
        n = len(ch["opened"]) + len(ch["closed"])
        rows.append(f"{dot} {_action(ch, short)}: {_title(ch, instr)} · {info}" + (f" · {n} Pos." if n > 1 else ""))
    dot, label, total = worst
    lines = [
        f"{dot} <b>{label}</b> · eToro · Gesamtrisiko bis {total:.0f}/100",
        _who(user, ti, profile),
        f"📋 <b>{len(changes)} Instrumente geändert:</b>",
        *rows,
    ]
    facts = trader_facts(profile)
    if facts:
        lines.append(f"Trader: {esc(facts)}")
    lines += _time_lines([p for ch in changes for p in ch["opened"]])
    lines.append(f"https://www.etoro.com/people/{esc(user)}/portfolio")
    return "\n".join(lines)
