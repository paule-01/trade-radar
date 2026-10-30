"""wikifolio: Depot-Änderungen in beobachteten wikifolios erkennen (Vergleich mit dem letzten Stand).

Große wikifolios handeln in vielen kleinen Teilorders; gemeldet wird deshalb nicht jeder Trade,
sondern die Änderung im Depot. Die Handelshistorie dient nur noch dem Trader-Profil.

wikifolio hat keine offizielle Lese-API. Genutzt werden die internen Endpunkte der
Webseite (wie im Open-Source-Projekt henrydatei/wikifolio-api). Für die Handelshistorie
ist ein kostenloses wikifolio-Konto nötig (Secrets WIKIFOLIO_EMAIL / WIKIFOLIO_PASSWORD).
Wenn wikifolio die Webseite umbaut, kann dieser Teil brechen – die anderen Teile laufen weiter.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone

from .common import env, esc, http_error, fmt_pct, get, load_state, log, save_state, send_telegram, session, to_float
from .risk import ASSET_LABEL, TraderProfile, ampel, score_trade, score_trader, trader_facts

BASE = "https://www.wikifolio.com"
PROFILE_MAX_AGE_H = 24
SECURITY_TYPE = {620: "stock", 630: "etf"}  # Gruppen im Depot; 0 = Cash (ohne Positionen)
PAGE_SIZE = 20  # mehr lässt wikifolio nicht zu ("Page size cannot be bigger than 20.")
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
        email, pw = env("WIKIFOLIO_EMAIL"), env("WIKIFOLIO_PASSWORD")
        if email and pw:
            try:
                r = self.s.post(f"{BASE}/api/login?country=de&language=de",
                                data={"email": email, "password": pw, "keepLoggedIn": True}, timeout=30)
                cookies = sorted(self.s.cookies.keys())
                self.logged_in = r.ok
                if r.ok:
                    log.info("wikifolio-Login: HTTP %s, Cookies %s", r.status_code, cookies)
                else:
                    log.error("wikifolio-Login fehlgeschlagen: %s", http_error(r))
            except Exception as e:
                log.error("wikifolio-Login: %s", e)

    def page_data(self, name: str) -> dict | None:
        r = get(self.s, f"{BASE}/de/de/w/{name}")
        if not r.ok:
            log.error("wikifolio-Seite %s -> %s", name, http_error(r))
            return None
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S)
        if not m:
            return None
        return json.loads(m.group(1))

    def trades(self, wf_id: str, count: int = PAGE_SIZE) -> list[dict]:
        """Neueste Trades (ohne Dividenden/Splits); blättert, da wikifolio max. 20 pro Seite liefert."""
        out: list[dict] = []
        page = 0
        while len(out) < count:
            r = get(self.s, f"{BASE}/api/wikifolio/{wf_id}/tradehistory",
                    params={"page": page, "pagesize": PAGE_SIZE, "country": "de", "language": "de"},
                    headers={"accept": "application/json"})
            if not r.ok:
                log.error("wikifolio tradehistory -> %s", http_error(r))
            r.raise_for_status()
            th = r.json().get("tradeHistory") or {}
            orders = th.get("orders") or []
            # Kapitalmaßnahmen (Dividende, Split) haben keinen orderType
            out += [o for o in orders if o.get("orderType")]
            page += 1
            if not orders or page >= (th.get("pageCount") or 0):
                break
            time.sleep(0.5)
        return out[:count]

    def portfolio(self, name: str) -> tuple[dict[str, dict], float | None]:
        """Aktuelles Depot: {ISIN: Position} über alle Gruppen (Aktien, ETFs, ...) und Depotwert."""
        r = get(self.s, f"{BASE}/api/wikifolio/{name}/portfolio",
                params={"country": "de", "language": "de"}, headers={"accept": "application/json"})
        if not r.ok:
            log.error("wikifolio portfolio %s -> %s", name, http_error(r))
        r.raise_for_status()
        return parse_portfolio(r.json())


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


def parse_portfolio(d: dict) -> tuple[dict[str, dict], float | None]:
    """Positionen aus allen Gruppen; percentage kommt als Bruch, wird zu Prozent."""
    out: dict[str, dict] = {}
    for g in d.get("groups") or []:
        for it in g.get("items") or []:
            key = it.get("isin") or it.get("name")
            if not key:
                continue
            w = to_float(it.get("percentage"))
            out[key] = {
                "name": it.get("name"), "isin": it.get("isin"),
                "qty": to_float(it.get("quantity")),
                "weight": w * 100 if w is not None else None,
                "price": to_float(it.get("mid")) or to_float(it.get("close")) or to_float(it.get("bid")),
                "type": SECURITY_TYPE.get(g.get("type")),
                "lev": bool(it.get("isLeveraged")),
            }
    return out, to_float(d.get("totalValue"))


def diff_portfolio(prev: dict, cur: dict, total: float | None, min_w: float, min_change: float) -> list[dict]:
    """Neu aufgenommen, komplett verkauft, aufgestockt/reduziert.

    Bei Aufstockung/Reduzierung zählt die Stückzahl: gehandelter Anteil = |ΔStück| × Kurs / Depotwert.
    So lösen reine Kursbewegungen keine Meldung aus. Ohne Stückzahl wird das Gewicht verglichen.
    """
    changes = []
    for k, c in cur.items():
        w = c.get("weight") or 0
        p = prev.get(k)
        if p is None:
            if w >= min_w:
                changes.append({"kind": "new", "pos": c, "old_w": None, "new_w": w})
            continue
        q0, q1 = p.get("qty"), c.get("qty")
        if q0 is not None and q1 is not None and total and c.get("price"):
            if q0 == q1:
                continue
            traded = abs(q1 - q0) * c["price"] / total * 100
            up = q1 > q0
        else:
            traded = abs(w - (p.get("weight") or 0))
            up = w > (p.get("weight") or 0)
        if traded >= min_change:
            changes.append({"kind": "add" if up else "reduce", "pos": c, "old_w": p.get("weight"),
                            "new_w": w, "old_qty": q0, "traded_pct": traded})
    for k, p in prev.items():
        if k not in cur and (p.get("weight") or 0) >= min_w:
            changes.append({"kind": "exit", "pos": p, "old_w": p.get("weight"), "new_w": None,
                            "old_qty": p.get("qty")})
    return changes


def run(cfg: dict, risk_cfg: dict) -> None:
    names = [n.lower() for n in (cfg.get("wikifolios") or [])]
    if not cfg.get("enabled", True) or not names:
        return
    api = Wikifolio()
    st = load_state("wikifolio")
    min_w = float(cfg.get("min_weight_pct", 1.0))
    min_change = float(cfg.get("min_change_pct", 1.5))
    msgs: list[str] = []

    for name in names:
        entry = st.get(name, {})
        try:
            # Profil (Handelsfrequenz, Hebelanteil) 1x täglich aus Seite + Handelshistorie
            if not entry.get("id") or time.time() - entry.get("profile_ts", 0) > PROFILE_MAX_AGE_H * 3600:
                nd = api.page_data(name)
                if not nd:
                    log.warning("wikifolio %s: Seite nicht lesbar", name)
                    continue
                wf_id = _dig(nd, "props", "pageProps", "data", "wikifolio", "id")
                prof, info = profile_from_page(nd, api.trades(wf_id, 100))
                entry.update({"id": wf_id, "info": info, "profile": prof.__dict__ | {"reasons": []},
                              "profile_ts": time.time()})
            cur, total = api.portfolio(name)
        except Exception as e:
            log.error("wikifolio %s: %s", name, e)
            continue
        entry.pop("seen", None)  # alter Stand aus der Meldung pro Trade

        prev = entry.get("positions")
        entry["positions"] = cur
        st[name] = entry
        if prev is None:  # erster Lauf: nur merken
            log.info("wikifolio %s: Ausgangszustand gespeichert (%d Positionen)", name, len(cur))
            continue
        changes = diff_portfolio(prev, cur, total, min_w, min_change)
        if changes:
            pd_ = {k: v for k, v in (entry.get("profile") or {}).items()
                   if k in TraderProfile.__dataclass_fields__ and k not in ("score", "label", "activity", "reasons")}
            profile = score_trader(TraderProfile(**pd_), risk_cfg)
            try:
                recent = api.trades(entry["id"])
            except Exception:
                recent = []
            for ch in changes:
                msgs.append(_msg(name, entry.get("info", {}), profile, ch, _last_trade(recent, ch), risk_cfg))
        time.sleep(2)  # wikifolio schonen

    save_state("wikifolio", st)
    for m in msgs:
        send_telegram(m)
    log.info("wikifolio: %d Meldungen", len(msgs))


def _last_trade(trades: list[dict], ch: dict) -> dict | None:
    """Neuester Trade der Historie zu dieser Position in passender Richtung."""
    want = "buy" if ch["kind"] in ("new", "add") else "sell"
    for t in trades:
        if t.get("isin") == ch["pos"].get("isin") and _side(t) == want:
            return t
    return None


def _msg(name, info, profile, ch, last, risk_cfg) -> str:
    pos, kind = ch["pos"], ch["kind"]
    asset = "leveraged" if pos.get("lev") else (pos.get("type") or _asset(pos))
    buy = kind in ("new", "add")
    tr = score_trade(asset, 1, False, ch["new_w"] if buy else None, None)
    if asset == "leveraged":
        tr.reasons = ["Hebelprodukt (Knock-out/Optionsschein o.ä.)"] + [r for r in tr.reasons if r != "Hebelprodukt"]
    dot, label, total = ampel(tr, profile, risk_cfg)
    action = {"new": "🆕 NEU IM DEPOT", "add": "➕ AUFGESTOCKT", "reduce": "➖ REDUZIERT",
              "exit": "🔻 KOMPLETT VERKAUFT"}[kind]
    share = f"Anteil {fmt_pct(ch['old_w']) if ch['old_w'] is not None else '–'} → " \
            f"{fmt_pct(ch['new_w']) if ch['new_w'] is not None else '0 %'}"
    details = [share]
    q0, q1 = ch.get("old_qty"), pos.get("qty") if kind != "exit" else 0
    if q0 is not None and q1 is not None and kind in ("add", "reduce", "exit"):
        details.append(f"Stück {q0:g} → {q1:g}".replace(".", ","))
    if kind in ("add", "reduce") and ch.get("traded_pct") is not None:
        details.append(f"gehandelt ca. {fmt_pct(ch['traded_pct'])} des Depots")
    lines = [
        f"{dot} <b>{label}</b> · wikifolio · Gesamtrisiko {total:.0f}/100",
        f"📁 <b>{esc(info.get('title') or name)}</b>"
        + (f" von {esc(info.get('trader'))}" if info.get("trader") else "")
        + f" – Profil: <b>{profile.label}</b>" + (f", {profile.activity}" if profile.activity else ""),
        f"{action}: <b>{esc(pos.get('name'))}</b>" + (f" ({esc(pos.get('isin'))})" if pos.get("isin") else "")
        + f" · {ASSET_LABEL.get(asset, 'Wertpapier')}",
        " · ".join(details),
    ]
    if last:
        price = to_float(last.get("executionPrice"))
        dt = _parse_dt(last.get("executionDate"))
        lines.append("Letzter Trade: " + " · ".join(x for x in [
            "Kauf" if buy else "Verkauf",
            f"{dt:%d.%m. %H:%M} UTC" if dt else "",
            f"Kurs {price:g}".replace(".", ",") if price else "",
        ] if x))
    lines.append(f"Trade-Risiko {tr.score:.0f}/100: {esc(', '.join(tr.reasons))}")
    facts = trader_facts(profile)
    if facts:
        lines.append(f"Depot: {esc(facts)}")
    lines.append(f"{BASE}/de/de/w/{esc(name)}")
    return "\n".join(lines)
