"""Große Fonds (13F, SEC): meldet Depot-Änderungen, sobald ein neuer Quartalsbericht erscheint."""
from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from collections import defaultdict

from .common import fail, esc, fmt_money, fmt_pct, get, load_state, log, save_state, session


def _sec():
    return session({"Accept": "application/json, text/xml, */*"})


def filings_13f(s, cik: int) -> list[dict]:
    r = get(s, f"https://data.sec.gov/submissions/CIK{cik:010d}.json")
    r.raise_for_status()
    rec = r.json()["filings"]["recent"]
    out = []
    for i, form in enumerate(rec["form"]):
        if form == "13F-HR":
            out.append({"acc": rec["accessionNumber"][i], "filed": rec["filingDate"][i],
                        "period": rec.get("reportDate", [""] * len(rec["form"]))[i]})
    return out  # neueste zuerst


def holdings(s, cik: int, acc: str) -> dict[str, dict]:
    base = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}"
    idx = get(s, f"{base}/index.json")
    idx.raise_for_status()
    names = [it["name"] for it in idx.json()["directory"]["item"]]
    xmls = [n for n in names if n.lower().endswith(".xml") and "primary_doc" not in n.lower()]
    if not xmls:
        raise RuntimeError("keine Info-Tabelle gefunden")
    time.sleep(0.2)
    r = get(s, f"{base}/{xmls[0]}")
    r.raise_for_status()
    return parse_infotable(r.content)


def parse_infotable(xml_bytes: bytes) -> dict[str, dict]:
    root = ET.fromstring(xml_bytes)

    def strip(t):
        return t.split("}", 1)[-1]

    agg: dict[str, dict] = defaultdict(lambda: {"name": "", "value": 0.0, "shares": 0.0})
    for el in root.iter():
        if strip(el.tag) != "infoTable":
            continue
        d = {}
        for c in el.iter():
            d[strip(c.tag)] = (c.text or "").strip()
        key = d.get("cusip", "") + ("/" + d["putCall"] if d.get("putCall") else "")
        a = agg[key]
        a["name"] = d.get("nameOfIssuer", "") + (f" ({d['putCall']})" if d.get("putCall") else "")
        a["value"] += float(d.get("value") or 0)
        a["shares"] += float(d.get("sshPrnamt") or 0)
    return dict(agg)


def diff(prev: dict, cur: dict) -> list[dict]:
    ch = []
    for k, c in cur.items():
        p = prev.get(k)
        if not p:
            ch.append({"kind": "new", "name": c["name"], "dv": c["value"], "value": c["value"]})
        elif p["shares"]:
            pct = (c["shares"] - p["shares"]) / p["shares"] * 100
            if abs(pct) >= 10:
                # Wert der gehandelten Stückzahl zum aktuellen Kurs
                price = c["value"] / c["shares"] if c["shares"] else 0
                dv = (c["shares"] - p["shares"]) * price
                ch.append({"kind": "buy" if pct > 0 else "sell", "name": c["name"], "pct": pct,
                           "dv": abs(dv), "value": c["value"]})
    for k, p in prev.items():
        if k not in cur:
            ch.append({"kind": "exit", "name": p["name"], "dv": p["value"], "value": 0})
    return sorted(ch, key=lambda x: -abs(x["dv"]))


def section(cfg: dict) -> str | None:
    if not cfg.get("enabled", True):
        return None
    s = _sec()
    st = load_state("funds13f")
    blocks = []
    for name, cik in (cfg.get("investors") or {}).items():
        cik = int(cik)
        try:
            fl = filings_13f(s, cik)
            time.sleep(0.2)
            if len(fl) < 2:
                continue
            latest = fl[0]
            if st.get(str(cik)) == latest["acc"]:
                continue
            first_run = str(cik) not in st
            st[str(cik)] = latest["acc"]
            if first_run:
                continue
            cur = holdings(s, cik, latest["acc"])
            prev = holdings(s, cik, fl[1]["acc"])
        except Exception as e:
            fail(f"13F {name}", e)
            continue
        changes = diff(prev, cur)[: int(cfg.get("max_changes_per_fund", 10))]
        total = sum(c["value"] for c in cur.values()) or 1
        lines = [f"<b>{esc(name)}</b> – Quartal bis {esc(latest['period'])}, gemeldet {esc(latest['filed'])}"]
        for c in changes:
            icon = {"new": "🆕 Neu", "exit": "❌ Verkauft", "buy": "➕ Aufgestockt", "sell": "➖ Reduziert"}[c["kind"]]
            pct = f" {fmt_pct(c['pct'], 0, sign=True)}" if "pct" in c else ""
            share = f", {fmt_pct(c['value'] / total * 100)} des Depots" if c["value"] else ""
            lines.append(f"  {icon}{pct}: {esc(c['name'])}{share}")
        blocks.append("\n".join(lines))
    save_state("funds13f", st)
    if not blocks:
        return None
    return ("🐋 <b>Neue Quartalsmeldungen großer Fonds (13F)</b>\n"
            "<i>Achtung: zeigt den Stand vom Quartalsende, also bis zu 45 Tage alt; Positionen im Depot "
            "großer Fonds sind meist konservativ bis ausgewogen</i>\n\n" + "\n\n".join(blocks))
