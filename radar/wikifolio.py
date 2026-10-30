"""wikifolio: neue Trades in beobachteten wikifolios erkennen.

wikifolio hat keine offizielle Lese-API. Genutzt werden die internen Endpunkte der
Webseite (wie im Open-Source-Projekt henrydatei/wikifolio-api). Für die Handelshistorie
ist ein kostenloses wikifolio-Konto nötig (Secrets WIKIFOLIO_EMAIL / WIKIFOLIO_PASSWORD).
Wenn wikifolio die Webseite umbaut, kann dieser Teil brechen – die anderen Teile laufen weiter.
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timedelta, timezone

from .common import esc, fmt_pct, get, load_state, log, save_state, send_telegram, session, to_float
from .risk import ASSET_LABEL, TraderProfile, ampel, score_trade, score_trader, trader_facts

BASE = "https://www.wikifolio.com"
PROFILE_MAX_AGE_H = 24
LEVER_WORDS = re.compile(r"turbo|knock|mini[ -]?(long|short|future)|optionsschein|faktor|warrant|"
                         r"hebel|\bko\b|open end|unlimited", re.I)


def _pct(v):
    f = to_float(v)
    if f is None:
        return None
    return f * 100 if abs(f) <= 1.0 else f


def _dig(d, *path):
    try:
        for p in path:
            d = d[p]
        return d
    except (KeyError, IndexError, TypeError):
        return None


def _find_key(obj, names: set[str]):
    """Sucht rekursiv den ersten Wert zu einem der Schlüssel."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in names and v not in (None, ""):
                return v
        for v in obj.values():
            r = _find_key(v, names)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_key(v, names)
            if r is not None:
                return r
    return None


class Wikifolio:
    def __init__(self):
        self.s = session({"accept": "application/json, text/html"})
        self.logged_in = False
        email, pw = os.environ.get("WIKIFOLIO_EMAIL"), os.environ.get("WIKIFOLIO_PASSWORD")
        if email and pw:
            try:
                r = self.s.post(f"{BASE}/api/login?country=de&language=de",
                                data={"email": email, "password": pw, "keepLoggedIn": True}, timeout=30)
                self.logged_in = r.ok
                if not r.ok:
                    log.error("wikifolio-Login fehlgeschlagen: HTTP %s", r.status_code)
            except Exception as e:
                log.error("wikifolio-Login: %s", e)

    def page_data(self, name: str) -> dict | None:
        r = get(self.s, f"{BASE}/de/de/w/{name}")
        if not r.ok:
            return None
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S)
        if not m:
            return None
        return json.loads(m.group(1))

    def trades(self, wf_id: str, page_size: int = 30) -> list[dict]:
        r = get(self.s, f"{BASE}/api/wikifolio/{wf_id}/tradehistory",
                params={"page": 0, "pagesize": page_size, "country": "de", "language": "de"},
                headers={"accept": "application/json"})
        r.raise_for_status()
        return _dig(r.json(), "tradeHistory", "orders") or []


def profile_from_page(nd: dict, trades: list[dict]) -> tuple[TraderProfile, dict]:
    data = _dig(nd, "props", "pageProps", "data") or {}
    kf = data.get("keyFigures") or {}
    risk = kf.get("otherKeyRiskIndicators")
    # Index 0 = seit Beginn, 3 = 1 Jahr; innere Rankings: 0 max. Verlust, 1 Risiko
    max_loss = _pct(_dig(risk, 0, "rankings", 0, "ranking", "value"))
    max_loss_1y = _pct(_dig(risk, 3, "rankings", 0, "ranking", "value"))
    wf = data.get("wikifolio") or {}
    info = {
        "id": wf.get("id"),
        "title": wf.get("title") or wf.get("shortDescription") or wf.get("name"),
        "trader": _find_key(wf, {"nickName", "nickname", "traderNickName"})
                  or _find_key(data, {"nickName", "nickname"}),
    }
    created = _find_key(data, {"creationDate", "createdDate", "emissionDate", "publishDate"})
    years = None
    if created:
        try:
            dt = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
            years = (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).days / 365.25
        except ValueError:
            pass
    perf_1y = _kpi(kf, "Performance (1 J")
    vol = _kpi(kf, "Volatilität (1 J", "Volatilität (3 J", "Volatilität (5 J", "Volatilität (Max")

    # Handelsfrequenz aus den letzten 90 Tagen
    tpm = None
    cutoff = datetime.now(timezone.utc) - timedelta(days=90)
    dated = [_parse_dt(t.get("executionDate")) for t in trades]
    dated = [d for d in dated if d]
    if dated:
        recent = sum(1 for d in dated if d >= cutoff)
        if len(dated) == len(trades) and min(dated) > cutoff:
            # alle geladenen Trades sind jünger als 90 Tage -> mind. so viele
            span_days = max(7, (datetime.now(timezone.utc) - min(dated)).days)
            tpm = len(dated) / span_days * 30
        else:
            tpm = recent / 3
    lev = [t for t in trades if t.get("isLeveraged")]
    p = TraderProfile(
        max_drawdown_pct=abs(max_loss) if max_loss is not None else (abs(max_loss_1y) if max_loss_1y else None),
        volatility_pct=abs(vol) if vol is not None else None,
        trades_per_month=tpm,
        high_leverage_pct=(len(lev) / len(trades) * 100) if trades else None,
        years_active=years,
        gain_1y_pct=perf_1y,
    )
    return p, info


def _kpi(kf: dict, *labels: str) -> float | None:
    """Kennzahl (als Bruch geliefert) über ihr Label suchen; erstes Label mit Wert gewinnt."""
    rankings = [x.get("ranking") or {} for x in kf.get("topKpis") or []]
    for group in kf.get("kpis") or []:
        rankings += [x.get("ranking") or {} for x in group.get("rankings") or []]
    for label in labels:
        for r in rankings:
            v = to_float(r.get("value"))
            if v is not None and str(r.get("label", "")).startswith(label):
                return v * 100
    return None


def _parse_dt(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _asset(t: dict) -> str:
    name = t.get("name") or ""
    if t.get("isLeveraged") or LEVER_WORDS.search(name):
        return "leveraged"
    if re.search(r"\bETF\b|\bETC\b|UCITS|iShares|Xtrackers|Vanguard|Amundi|SPDR", name, re.I):
        return "etf"
    if re.search(r"bitcoin|ethereum|crypto|krypto", name, re.I):
        return "crypto"
    return "stock"


def _side(t: dict) -> str:
    ot = str(t.get("orderType") or "").lower()
    if "sell" in ot or "verkauf" in ot:
        return "sell"
    if "buy" in ot or "kauf" in ot:
        return "buy"
    return "?"


def run(cfg: dict, risk_cfg: dict) -> None:
    names = [n.lower() for n in (cfg.get("wikifolios") or [])]
    if not cfg.get("enabled", True) or not names:
        return
    api = Wikifolio()
    st = load_state("wikifolio")
    min_w = float(cfg.get("min_weight_pct", 0.5))
    msgs: list[str] = []

    for name in names:
        entry = st.get(name, {})
        try:
            if not entry.get("id") or time.time() - entry.get("profile_ts", 0) > PROFILE_MAX_AGE_H * 3600:
                nd = api.page_data(name)
                if not nd:
                    log.warning("wikifolio %s: Seite nicht lesbar", name)
                    continue
                wf_id = _dig(nd, "props", "pageProps", "data", "wikifolio", "id")
                trades = api.trades(wf_id, 100)
                prof, info = profile_from_page(nd, trades)
                entry.update({"id": wf_id, "info": info, "profile": prof.__dict__ | {"reasons": []},
                              "profile_ts": time.time()})
            else:
                trades = api.trades(entry["id"], 30)
        except Exception as e:
            log.error("wikifolio %s: %s", name, e)
            continue

        pd_ = {k: v for k, v in (entry.get("profile") or {}).items()
               if k in TraderProfile.__dataclass_fields__ and k not in ("score", "label", "activity", "reasons")}
        profile = score_trader(TraderProfile(**pd_), risk_cfg)
        info = entry.get("info", {})

        seen = set(entry.get("seen", []))
        ids = [str(t.get("id")) for t in trades]
        if not seen:  # erster Lauf: nur merken
            entry["seen"] = ids[:300]
            st[name] = entry
            log.info("wikifolio %s: Ausgangszustand gespeichert", name)
            continue
        for t in reversed(trades):  # älteste zuerst melden
            tid = str(t.get("id"))
            if tid in seen:
                continue
            w = _pct(t.get("weightage"))
            if w is not None and abs(w) < min_w:
                continue
            msgs.append(_msg(name, info, profile, t, w, risk_cfg))
        entry["seen"] = (ids + [i for i in entry.get("seen", []) if i not in ids])[:300]
        st[name] = entry
        time.sleep(2)  # wikifolio schonen

    save_state("wikifolio", st)
    for m in msgs:
        send_telegram(m)
    log.info("wikifolio: %d Meldungen", len(msgs))


def _msg(name, info, profile, t, weight, risk_cfg) -> str:
    side = _side(t)
    asset = _asset(t)
    tr = score_trade(asset, 1, False, weight if side == "buy" else None, None)
    if asset == "leveraged":
        tr.reasons = ["Hebelprodukt (Knock-out/Optionsschein o.ä.)"] + [r for r in tr.reasons if r != "Hebelprodukt"]
    dot, label, total = ampel(tr, profile, risk_cfg)
    action = {"buy": "🆕 GEKAUFT", "sell": "🔻 VERKAUFT"}.get(side, f"↔️ {esc(t.get('orderType'))}")
    price = to_float(t.get("executionPrice"))
    dt = _parse_dt(t.get("executionDate"))
    lines = [
        f"{dot} <b>{label}</b> · wikifolio · Gesamtrisiko {total:.0f}/100",
        f"📁 <b>{esc(info.get('title') or name)}</b>"
        + (f" von {esc(info.get('trader'))}" if info.get("trader") else "")
        + f" – Profil: <b>{profile.label}</b>" + (f", {profile.activity}" if profile.activity else ""),
        f"{action}: <b>{esc(t.get('name'))}</b> ({esc(t.get('isin'))}) · {ASSET_LABEL.get(asset)}",
        " · ".join(x for x in [
            f"Kurs {price:g}".replace(".", ",") if price else "",
            f"{fmt_pct(weight)} Gewicht im Depot" if weight is not None else "",
            f"{dt:%d.%m. %H:%M} UTC" if dt else "",
        ] if x),
        f"Trade-Risiko {tr.score:.0f}/100: {esc(', '.join(tr.reasons))}",
    ]
    facts = trader_facts(profile)
    if facts:
        lines.append(f"Depot: {esc(facts)}")
    lines.append(f"{BASE}/de/de/w/{esc(name)}")
    return "\n".join(lines)
