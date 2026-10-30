"""ARK Invest (Cathie Wood): tägliche Käufe/Verkäufe aus den Bestandslisten ableiten."""
from __future__ import annotations

import csv
import io

from .common import fail, esc, fmt_money, fmt_pct, get, load_state, log, save_state, session, to_float

FILES = {
    "ARKK": "ARK_INNOVATION_ETF_ARKK_HOLDINGS.csv",
    "ARKW": "ARK_NEXT_GENERATION_INTERNET_ETF_ARKW_HOLDINGS.csv",
    "ARKG": "ARK_GENOMIC_REVOLUTION_ETF_ARKG_HOLDINGS.csv",
    "ARKQ": "ARK_AUTONOMOUS_TECH._&_ROBOTICS_ETF_ARKQ_HOLDINGS.csv",
    "ARKF": "ARK_FINTECH_INNOVATION_ETF_ARKF_HOLDINGS.csv",
    "ARKX": "ARK_SPACE_EXPLORATION_&_INNOVATION_ETF_ARKX_HOLDINGS.csv",
}
CSV_URL = "https://assets.ark-funds.com/fund-documents/funds-etf-csv/{}"
FALLBACK = "https://arkfunds.io/api/v2/etf/holdings?symbol={}"
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")


def parse_csv(text: str) -> tuple[str, dict[str, dict]]:
    rows = list(csv.DictReader(io.StringIO(text)))
    out, date = {}, ""
    for r in rows:
        r = {(k or "").strip().lower(): (v or "").strip() for k, v in r.items()}
        key = r.get("ticker") or r.get("cusip")
        shares = to_float(r.get("shares"))
        if not key or shares is None:
            continue
        date = date or r.get("date", "")
        out[key] = {"company": r.get("company", key), "shares": shares,
                    "value": to_float(r.get("market value ($)") or r.get("market value")),
                    "weight": to_float(r.get("weight (%)") or r.get("weight"))}
    return date, out


def fetch(fund: str) -> tuple[str, dict[str, dict]]:
    s = session({"User-Agent": BROWSER_UA})
    try:
        r = get(s, CSV_URL.format(FILES[fund]))
        if r.ok and "ticker" in r.text[:500].lower():
            return parse_csv(r.text)
    except Exception as e:
        log.info("ARK-CSV %s: %s – nutze Ersatzquelle", fund, e)
    r = get(s, FALLBACK.format(fund))
    r.raise_for_status()
    j = r.json()
    out = {}
    for h in j.get("holdings", []):
        key = h.get("ticker") or h.get("cusip")
        if key:
            out[key] = {"company": h.get("company"), "shares": to_float(h.get("shares")),
                        "value": to_float(h.get("market_value")), "weight": to_float(h.get("weight"))}
    return str(j.get("date_to") or (j.get("holdings") or [{}])[0].get("date", "")), out


def diff(prev: dict, cur: dict, min_change_pct: float) -> list[dict]:
    ch = []
    for k, c in cur.items():
        p = prev.get(k)
        if not p:
            ch.append({"ticker": k, "kind": "new", **c})
        else:
            d = c["shares"] - p["shares"]
            if p["shares"] and abs(d) / p["shares"] * 100 >= min_change_pct:
                ch.append({"ticker": k, "kind": "buy" if d > 0 else "sell",
                           "pct": d / p["shares"] * 100, **c})
    for k, p in prev.items():
        if k not in cur:
            ch.append({"ticker": k, "kind": "exit", **p})
    order = {"new": 0, "exit": 1, "buy": 2, "sell": 3}
    return sorted(ch, key=lambda x: (order[x["kind"]], -abs(x.get("pct", 0))))


def section(cfg: dict) -> str | None:
    if not cfg.get("enabled", True):
        return None
    st = load_state("ark")
    lines = []
    for fund in cfg.get("funds", list(FILES)):
        if fund not in FILES:
            continue
        try:
            date, cur = fetch(fund)
        except Exception as e:
            fail(f"ARK {fund}", e)
            continue
        prev = st.get(fund)
        st[fund] = {"date": date, "holdings": cur}
        if not prev or prev.get("date") == date:
            continue
        changes = diff(prev["holdings"], cur, float(cfg.get("min_change_pct", 5)))
        if not changes:
            continue
        lines.append(f"<b>{fund}</b> (Stand {esc(date)})")
        for c in changes[:12]:
            icon = {"new": "🆕 Neu", "exit": "❌ Komplett verkauft", "buy": "➕ Aufgestockt",
                    "sell": "➖ Reduziert"}[c["kind"]]
            pct = f" {fmt_pct(c['pct'], 0, sign=True)}" if "pct" in c else ""
            w = f", {fmt_pct(c.get('weight'))} des Fonds" if c.get("weight") is not None and c["kind"] != "exit" else ""
            lines.append(f"  {icon}{pct}: {esc(c['company'])} ({esc(c['ticker'])}){w}")
    save_state("ark", st)
    if not lines:
        return None
    return ("🚀 <b>ARK Invest (Cathie Wood)</b>\n"
            "<i>Einordnung: 🟡/🔴 wachstumsorientiert, stark schwankende Tech-Werte</i>\n" + "\n".join(lines))
