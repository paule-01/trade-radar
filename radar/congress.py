"""US-Kongress: neue Aktien-Meldungen (STOCK Act) von Abgeordneten und Senatoren.

Quellen sind die offiziellen Register:
- Repräsentantenhaus: disclosures-clerk.house.gov (Index als ZIP, Meldungen als PDF)
- Senat: efdsearch.senate.gov (HTML)
"""
from __future__ import annotations

import io
import re
import time
import xml.etree.ElementTree as ET
import zipfile
from datetime import date, datetime, timedelta

from bs4 import BeautifulSoup

from .common import fail, esc, get, load_state, log, save_state, session

HOUSE_ZIP = "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{y}FD.zip"
HOUSE_PDF = "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{y}/{doc}.pdf"
SENATE = "https://efdsearch.senate.gov"
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")
TX_TYPE = {"P": "Kauf", "S": "Verkauf", "S (partial)": "Teilverkauf", "E": "Tausch"}


# ------------------------------------------------------------------ House
def parse_house_index(xml_bytes: bytes) -> list[dict]:
    root = ET.fromstring(xml_bytes)
    out = []
    for m in root.iter("Member"):
        d = {c.tag: (c.text or "").strip() for c in m}
        if d.get("FilingType") == "P":
            out.append(d)
    return out


def parse_house_pdf(text: str) -> list[dict]:
    """Holt Ticker, Art (P/S) und Betragsspanne aus dem PDF-Text (best effort)."""
    txs = []
    flat = re.sub(r"\s+", " ", text)
    pat = re.compile(r"\(([A-Z][A-Z.\-]{0,6})\)\s*(?:\[[A-Z]{2}\])?\s*(P|S \(partial\)|S|E)\s+"
                     r"(\d{2}/\d{2}/\d{4})\s+\d{2}/\d{2}/\d{4}\s+(\$[\d,]+(?:\s*-\s*\$[\d,]+)?|Over \$[\d,]+)")
    for m in pat.finditer(flat):
        txs.append({"ticker": m.group(1), "type": TX_TYPE.get(m.group(2), m.group(2)),
                    "date": m.group(3), "amount": m.group(4).replace(" ", "")})
    return txs


def house(only: set[str], seen: set[str], baseline: bool = False) -> list[dict]:
    s = session({"User-Agent": BROWSER_UA})
    y = date.today().year
    filings = []
    for year in {y, (date.today() - timedelta(days=10)).year}:
        r = get(s, HOUSE_ZIP.format(y=year))
        if not r.ok:
            continue
        z = zipfile.ZipFile(io.BytesIO(r.content))
        xml_name = next((n for n in z.namelist() if n.lower().endswith(".xml")), None)
        if xml_name:
            filings += [f | {"_year": year} for f in parse_house_index(z.read(xml_name))]
    out = []
    for f in filings:
        doc = f.get("DocID")
        if not doc or f"H{doc}" in seen:
            continue
        if only and f.get("Last", "").lower() not in only:
            seen.add(f"H{doc}")
            continue
        seen.add(f"H{doc}")
        if baseline:
            continue
        url = HOUSE_PDF.format(y=f["_year"], doc=doc)
        txs = []
        try:
            import pdfplumber
            pr = get(s, url)
            if pr.ok and pr.content[:4] == b"%PDF":
                with pdfplumber.open(io.BytesIO(pr.content)) as pdf:
                    txs = parse_house_pdf("\n".join(p.extract_text() or "" for p in pdf.pages))
            time.sleep(0.3)
        except Exception as e:
            log.info("House-PDF %s: %s", doc, e)
        name = " ".join(x for x in (f.get("Prefix"), f.get("First"), f.get("Last")) if x)
        out.append({"who": f"{name} ({f.get('StateDst', '')})", "filed": f.get("FilingDate"),
                    "url": url, "txs": txs, "chamber": "Repräsentantenhaus"})
    return out


# ------------------------------------------------------------------ Senate
def senate(only: set[str], seen: set[str], since_days: int = 5, baseline: bool = False) -> list[dict]:
    s = session({"User-Agent": BROWSER_UA})
    r = get(s, f"{SENATE}/search/home/")
    if r.status_code == 403:
        raise RuntimeError("Zugriff gesperrt (HTTP 403, Bot-Schutz der Senats-Webseite)")
    soup = BeautifulSoup(r.text, "html.parser")
    tok = soup.find("input", {"name": "csrfmiddlewaretoken"})
    if not tok:
        raise RuntimeError("kein CSRF-Token")
    s.post(f"{SENATE}/search/home/", data={"prohibition_agreement": "1",
                                            "csrfmiddlewaretoken": tok["value"]},
           headers={"Referer": f"{SENATE}/search/home/"}, timeout=30)
    csrf = s.cookies.get("csrftoken") or s.cookies.get("33a5c6d97f299a223cb6fc3925909ef7")
    start = (datetime.now() - timedelta(days=since_days)).strftime("%m/%d/%Y 00:00:00")
    r = s.post(f"{SENATE}/search/report/data/", timeout=30,
               headers={"Referer": f"{SENATE}/search/", "X-CSRFToken": csrf or ""},
               data={"start": "0", "length": "100", "report_types": "[11]", "filer_types": "[]",
                     "submitted_start_date": start, "submitted_end_date": "", "candidate_state": "",
                     "senator_state": "", "office_id": "", "first_name": "", "last_name": "",
                     "csrfmiddlewaretoken": csrf or ""})
    r.raise_for_status()
    out = []
    for row in r.json().get("data", []):
        first, last, office, link_html, filed = (row + [""] * 5)[:5]
        href = BeautifulSoup(link_html, "html.parser").a
        if not href:
            continue
        path = href["href"]
        if f"S{path}" in seen:
            continue
        seen.add(f"S{path}")
        if baseline or (only and str(last).lower() not in only):
            continue
        url = SENATE + path
        txs = []
        if "/ptr/" in path:
            try:
                pr = get(s, url)
                txs = parse_senate_ptr(pr.text)
                time.sleep(0.3)
            except Exception as e:
                log.info("Senat-PTR %s: %s", path, e)
        out.append({"who": f"Sen. {first} {last}".strip(), "filed": filed, "url": url,
                    "txs": txs, "chamber": "Senat"})
    return out


def parse_senate_ptr(html_text: str) -> list[dict]:
    soup = BeautifulSoup(html_text, "html.parser")
    table = soup.find("table")
    if not table:
        return []
    heads = [th.get_text(" ", strip=True).lower() for th in table.find_all("th")]
    txs = []
    for tr in table.find_all("tr")[1:]:
        cells = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
        if len(cells) != len(heads):
            continue
        d = dict(zip(heads, cells))
        typ = d.get("type", "")
        txs.append({"ticker": d.get("ticker", "--"), "type": "Kauf" if "Purchase" in typ else
                    "Verkauf" if "Sale" in typ else typ, "date": d.get("transaction date", ""),
                    "amount": d.get("amount", ""), "asset": d.get("asset name", "")})
    return txs


# ------------------------------------------------------------------ Abschnitt
def section(cfg: dict) -> str | None:
    if not cfg.get("enabled", True):
        return None
    only = {n.lower() for n in (cfg.get("only_members") or [])}
    st = load_state("congress")
    first_run = not st.get("seen")
    seen = set(st.get("seen", []))
    found = []
    if cfg.get("house", True):
        try:
            found += house(only, seen, first_run)
        except Exception as e:
            fail("Repräsentantenhaus", e)
    if cfg.get("senate", True):
        try:
            # eigener Ausgangszustand: klappt der Senat erst später, nicht alles nachmelden
            found += senate(only, seen, baseline=first_run or not st.get("senate_ok"))
            st["senate_ok"] = True
        except Exception as e:
            fail("Senat", e)
    st["seen"] = list(seen)[-6000:]
    save_state("congress", st)
    if first_run:  # beim allerersten Lauf nicht das ganze Jahr melden
        log.info("Kongress: Ausgangszustand gespeichert (%d Meldungen)", len(seen))
        return None
    if not found:
        return None
    lines = ["🏛 <b>US-Politiker-Trades</b> (neue Pflichtmeldungen)",
             "<i>Hinweis: Trades liegen oft bis zu 45 Tage zurück</i>"]
    for f in found[:20]:
        lines.append(f"• <b>{esc(f['who'])}</b> – {esc(f['chamber'])}, gemeldet {esc(f['filed'])}")
        for t in f["txs"][:6]:
            lines.append(f"    {'🟢' if t['type'] == 'Kauf' else '🔻'} {esc(t['type'])} "
                         f"<b>{esc(t['ticker'])}</b> {esc(t['amount'])} ({esc(t['date'])})")
        if len(f["txs"]) > 6:
            lines.append(f"    … und {len(f['txs']) - 6} weitere")
        lines.append(f"    {esc(f['url'])}")
    return "\n".join(lines)
