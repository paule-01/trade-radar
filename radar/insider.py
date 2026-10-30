"""Insider-Käufe: USA (SEC Form 4 über OpenInsider) und Deutschland (BaFin Directors' Dealings)."""
from __future__ import annotations

import csv
import io
import re
from collections import defaultdict

from bs4 import BeautifulSoup

from .common import fail, esc, fmt_money, get, load_state, log, save_state, session, to_float

OPENINSIDER = ("http://openinsider.com/screener?s=&o=&pl=&ph=&ll=&lh=&fd=3&fdr=&td=0&tdr=&fdlyl=&fdlyh="
               "&daysago=&xp=1&vl={vl}&vh=&ocl=&och=&sic1=-1&sicl=100&sich=9999&grp=0&nfl=&nfh=&nil=&nih="
               "&nol=&noh=&v2l=&v2h=&oc2l=&oc2h=&sortcol=0&cnt=200&page=1")
BAFIN = ("https://portal.mvp.bafin.de/database/DealingsInfo/sucheForm.do?meldepflichtigerName=&zeitraum=1"
         "&d-4000784-e=1&emittentButton=Suche+Emittent&emittentName=&zeitraumVon=&emittentIsin="
         "&6578706f7274=1&zeitraumBis=")


# ------------------------------------------------------------------ USA
def parse_openinsider(html_text: str) -> list[dict]:
    soup = BeautifulSoup(html_text, "html.parser")
    table = soup.find("table", class_="tinytable")
    if not table:
        return []
    heads = [th.get_text(" ", strip=True).replace("\xa0", " ") for th in table.find_all("th")]
    rows = []
    for tr in table.find("tbody").find_all("tr") if table.find("tbody") else []:
        cells = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
        if len(cells) != len(heads):
            continue
        rows.append(dict(zip(heads, cells)))
    return rows


def us_section(cfg: dict) -> str | None:
    if not cfg.get("enabled", True):
        return None
    min_v = float(cfg.get("min_value_usd", 250000))
    s = session({"User-Agent": "Mozilla/5.0 TradeRadar"})
    try:
        r = get(s, OPENINSIDER.format(vl=int(min_v // 1000)))
        r.raise_for_status()
        rows = parse_openinsider(r.text)
    except Exception as e:
        fail("Insider USA (OpenInsider)", e)
        return None
    st = load_state("insider_us")
    seen = set(st.get("seen", []))
    new = []
    for row in rows:
        if "P" not in (row.get("Trade Type") or "")[:2]:
            continue
        key = "|".join(row.get(k, "") for k in ("Filing Date", "Ticker", "Insider Name", "Qty"))
        if key in seen:
            continue
        seen.add(key)
        v = to_float(row.get("Value"))
        if v is None or v < min_v:
            continue
        new.append(row | {"_v": v})
    st["seen"] = list(seen)[-3000:]
    save_state("insider_us", st)
    if not new:
        return None

    by_ticker: dict[str, list] = defaultdict(list)
    for row in new:
        by_ticker[row.get("Ticker", "?")].append(row)
    ranked = sorted(by_ticker.items(), key=lambda kv: (-len(kv[1]), -sum(r["_v"] for r in kv[1])))
    lines = ["🇺🇸 <b>Insider-Käufe USA</b> (Vorstände/Direktoren kaufen eigene Aktien)",
             "<i>Einordnung: Käufe mehrerer Insider gleichzeitig (Cluster) gelten als stärkstes Signal</i>"]
    for ticker, rs in ranked[: int(cfg.get("max_items", 15))]:
        total = sum(r["_v"] for r in rs)
        comp = rs[0].get("Company Name", "")
        who = ", ".join(sorted({f"{r.get('Insider Name')} ({r.get('Title')})" for r in rs}))[:180]
        tag = f"👥 Cluster ({len(rs)} Insider) " if len({r.get('Insider Name') for r in rs}) > 1 else ""
        own = rs[0].get("ΔOwn") or ""
        lines.append(f"• {tag}<b>{esc(ticker)}</b> {esc(comp)} – {fmt_money(total)}"
                     + (f", Bestand {esc(own)}" if own else "")
                     + f"\n  {esc(who)}\n  http://openinsider.com/{esc(ticker)}")
    return "\n".join(lines)


# ------------------------------------------------------------------ Deutschland
def parse_bafin(text: str) -> list[dict]:
    sample = text[:2000]
    delim = ";" if sample.count(";") >= sample.count(",") else ","
    rows = []
    for r in csv.DictReader(io.StringIO(text), delimiter=delim):
        rows.append({(k or "").strip(): (v or "").strip() for k, v in r.items()})
    return rows


def _col(row: dict, *needles: str) -> str:
    for k, v in row.items():
        lk = k.lower()
        if all(n in lk for n in needles):
            return v
    return ""


def de_section(cfg: dict) -> str | None:
    if not cfg.get("enabled", True):
        return None
    min_v = float(cfg.get("min_value_eur", 100000))
    s = session({"User-Agent": "Mozilla/5.0 TradeRadar"})
    try:
        r = get(s, BAFIN)
        r.raise_for_status()
        raw = r.content
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("cp1252", errors="replace")
        rows = parse_bafin(text)
    except Exception as e:
        fail("Insider DE (BaFin)", e)
        return None
    st = load_state("insider_de")
    seen = set(st.get("seen", []))
    items = []
    for row in rows:
        art = _col(row, "art des gesch").lower()
        if "kauf" not in art or "verkauf" in art:
            continue
        vol_raw = _col(row, "volumen")
        vol = to_float(re.sub(r"[^\d,.\-]", "", vol_raw))
        if vol is None or vol < min_v:
            continue
        key = "|".join([_col(row, "emittent"), _col(row, "meldepflichtig"), _col(row, "datum des gesch"), vol_raw])
        if key in seen:
            continue
        seen.add(key)
        items.append({"emittent": _col(row, "emittent"), "isin": _col(row, "isin"),
                      "wer": _col(row, "meldepflichtig"), "pos": _col(row, "position"),
                      "vol": vol, "datum": _col(row, "datum des gesch"),
                      "preis": _col(row, "preis")})
    st["seen"] = list(seen)[-3000:]
    save_state("insider_de", st)
    if not items:
        return None
    items.sort(key=lambda x: -x["vol"])
    lines = ["🇩🇪 <b>Directors' Dealings Deutschland</b> (Insider-Käufe, BaFin)"]
    for it in items[: int(cfg.get("max_items", 15))]:
        lines.append(f"• <b>{esc(it['emittent'])}</b> ({esc(it['isin'])}) – {fmt_money(it['vol'], '€')}"
                     f" zu {esc(it['preis'])}, {esc(it['datum'])}\n  {esc(it['wer'])} ({esc(it['pos'])})")
    return "\n".join(lines)
