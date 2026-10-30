"""Offline-Tests mit Beispieldaten (kein Internet nötig): python -m pytest -q"""
import json
import os

import pytest

os.environ["DRY_RUN"] = "1"


@pytest.fixture(autouse=True)
def tmp_state(tmp_path, monkeypatch):
    from radar import common
    monkeypatch.setattr(common, "STATE_DIR", tmp_path)
    yield tmp_path


def test_risk_profiles():
    from radar.risk import TraderProfile, ampel, score_trade, score_trader
    calm = score_trader(TraderProfile(platform_risk=3, max_drawdown_pct=12, trades_per_month=3,
                                      high_leverage_pct=0, years_active=6))
    wild = score_trader(TraderProfile(platform_risk=7, max_drawdown_pct=45, trades_per_month=90,
                                      high_leverage_pct=40, years_active=0.8))
    assert calm.label == "konservativ" and calm.activity.startswith("Langfrist")
    assert wild.label == "risikofreudig" and wild.activity == "Vieltrader"
    assert ampel(score_trade("etf"), calm)[1] == "KONSERVATIV"
    assert ampel(score_trade("crypto", leverage=5, position_pct=25), wild)[1] == "RISKANT"


def test_etoro_flow(monkeypatch, capsys):
    from radar import etoro
    monkeypatch.setenv("ETORO_API_KEY", "x")
    portfolios = {"alice": {"positions": [
        {"positionId": 1, "instrumentId": 1001, "isBuy": True, "leverage": 1, "investmentPct": 12.0,
         "openRate": 180.5, "netProfit": 3.2, "stopLossRate": 0, "openTimestamp": "2026-10-01T10:00:00Z"}],
        "socialTrades": []}}

    class Fake:
        def rankings(self, *a):
            return [{"username": "Alice", "gain": 0.31}]

        def live_portfolio(self, u):
            return portfolios[u]

        def trade_info(self, u, period="OneYearAgo"):
            return {"fullName": "Alice A", "riskScore": 4, "maxMonthlyRiskScore": 5, "peakToValley": -0.18,
                    "trades": 60, "highLeveragePct": 0.02, "winRatio": 0.64, "profitableMonthsPct": 0.7,
                    "weeksSinceRegistration": 260, "copiers": 4200}

        def instruments(self, ids):
            return {1001: {"name": "Apple", "symbol": "AAPL", "type": "stock"},
                    100000: {"name": "Bitcoin", "symbol": "BTC", "type": "crypto"}}

    monkeypatch.setattr(etoro, "EToro", Fake)
    monkeypatch.setattr(etoro.time, "sleep", lambda s: None)
    cfg = {"enabled": True, "traders": [], "auto_discover": {"enabled": True}, "min_position_pct": 0.5}
    etoro.run(cfg, {})
    assert "TELEGRAM" not in capsys.readouterr().out  # erster Lauf: nur Ausgangszustand

    portfolios["alice"]["positions"] = [
        {"positionId": 2, "instrumentId": 1001, "isBuy": True, "leverage": 1, "investmentPct": 5.0,
         "openRate": 190.1, "openTimestamp": "2026-10-30T09:15:00Z"},
        {"positionId": 3, "instrumentId": 100000, "isBuy": True, "leverage": 2, "investmentPct": 15.0,
         "openRate": 70000, "stopLossRate": 0, "openTimestamp": "2026-10-30T09:20:00Z"}]
    etoro.run(cfg, {})
    out = capsys.readouterr().out
    print(out)
    assert "NEU GEKAUFT" in out and "Bitcoin" in out and "Hebel x2" in out
    assert "UMGESCHICHTET" in out and "Apple" in out  # Apple 1 zu, Apple 2 auf -> eine Meldung
    assert "Profil: <b>" in out and "eToro-Risiko 4/10" in out


def test_wikifolio_helpers():
    from radar import wikifolio as w
    assert w._side({"orderType": "Verkauf"}) == "sell"
    assert w._side({"orderType": "Buy"}) == "buy"
    assert w._asset({"name": "Turbo Long Open End auf DAX"}) == "leveraged"
    assert w._asset({"name": "iShares Core MSCI World UCITS ETF"}) == "etf"
    nd = {"props": {"pageProps": {"data": {"wikifolio": {"id": "abc", "title": "Test"},
          "keyFigures": {"otherKeyRiskIndicators": [{"rankings": [{"ranking": {"value": -0.22}}]}]}}}}}
    p, info = w.profile_from_page(nd, [])
    assert round(p.max_drawdown_pct) == 22 and info["id"] == "abc"


def test_ark_diff():
    from radar import ark
    csv1 = 'date,fund,company,ticker,cusip,shares,"market value ($)","weight (%)"\n' \
           '10/29/2026,ARKK,TESLA INC,TSLA,1,"1,000,000","$250,000,000.00",10.00%\n' \
           '10/29/2026,ARKK,ROKU INC,ROKU,2,"500,000","$30,000,000.00",2.00%\n'
    csv2 = 'date,fund,company,ticker,cusip,shares,"market value ($)","weight (%)"\n' \
           '10/30/2026,ARKK,TESLA INC,TSLA,1,"900,000","$225,000,000.00",9.00%\n' \
           '10/30/2026,ARKK,PALANTIR,PLTR,3,"100,000","$4,000,000.00",0.50%\n'
    d1, h1 = ark.parse_csv(csv1)
    d2, h2 = ark.parse_csv(csv2)
    kinds = {c["ticker"]: c["kind"] for c in ark.diff(h1, h2, 5)}
    assert kinds == {"TSLA": "sell", "ROKU": "exit", "PLTR": "new"}


def test_openinsider_parse():
    from radar.insider import parse_openinsider
    html = """<table class="tinytable"><thead><tr><th>X</th><th>Filing Date</th><th>Trade Date</th>
    <th>Ticker</th><th>Company Name</th><th>Insider Name</th><th>Title</th><th>Trade Type</th><th>Price</th>
    <th>Qty</th><th>Owned</th><th>ΔOwn</th><th>Value</th></tr></thead><tbody><tr><td></td>
    <td>2026-10-29 16:01:02</td><td>2026-10-28</td><td>ACME</td><td>Acme Corp</td><td>Doe John</td>
    <td>CEO</td><td>P - Purchase</td><td>$10.00</td><td>+50,000</td><td>1,050,000</td><td>+5%</td>
    <td>+$500,000</td></tr></tbody></table>"""
    rows = parse_openinsider(html)
    assert rows[0]["Ticker"] == "ACME" and rows[0]["Value"] == "+$500,000"


def test_bafin_parse():
    from radar.insider import _col, parse_bafin
    from radar.common import to_float
    txt = ("Emittent;BaFin-ID;ISIN;Meldepflichtiger;Position / Status;Art des Instruments;Art des Geschäfts;"
           "Durchschnittspreis;Aggregiertes Volumen;Mitteilungsdatum;Datum des Geschäfts\n"
           "Muster AG;123;DE0001;Max Muster;Vorstand;Aktie;Kauf;12,50 EUR;250.000,00 EUR;29.10.2026;28.10.2026\n")
    r = parse_bafin(txt)[0]
    assert _col(r, "art des gesch") == "Kauf"
    assert to_float("250.000,00") == 250000.0


def test_house_pdf_regex():
    from radar.congress import parse_house_pdf
    text = ("SP NVIDIA Corporation - Common Stock (NVDA) [ST] P 10/01/2026 10/02/2026 $250,001 - $500,000\n"
            "Apple Inc. (AAPL) [ST] S (partial) 09/20/2026 09/22/2026 $15,001 - $50,000")
    tx = parse_house_pdf(text)
    assert [(t["ticker"], t["type"]) for t in tx] == [("NVDA", "Kauf"), ("AAPL", "Teilverkauf")]


def test_senate_ptr_parse():
    from radar.congress import parse_senate_ptr
    html = """<table><tr><th>#</th><th>Transaction Date</th><th>Owner</th><th>Ticker</th><th>Asset Name</th>
    <th>Asset Type</th><th>Type</th><th>Amount</th><th>Comment</th></tr><tr><td>1</td><td>10/01/2026</td>
    <td>Self</td><td>MSFT</td><td>Microsoft</td><td>Stock</td><td>Purchase</td><td>$1,001 - $15,000</td>
    <td>--</td></tr></table>"""
    assert parse_senate_ptr(html)[0]["type"] == "Kauf"


def test_13f_parse_diff():
    from radar.funds13f import diff, parse_infotable
    def xml(rows):
        items = "".join(f"<infoTable><nameOfIssuer>{n}</nameOfIssuer><cusip>{c}</cusip><value>{v}</value>"
                        f"<shrsOrPrnAmt><sshPrnamt>{s}</sshPrnamt></shrsOrPrnAmt></infoTable>" for n, c, v, s in rows)
        return f'<informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable">{items}</informationTable>'.encode()
    prev = parse_infotable(xml([("APPLE", "A1", 1000, 100), ("BANK", "B1", 500, 50)]))
    cur = parse_infotable(xml([("APPLE", "A1", 600, 60), ("CHUBB", "C1", 300, 10)]))
    kinds = {c["name"]: c["kind"] for c in diff(prev, cur)}
    assert kinds == {"APPLE": "sell", "BANK": "exit", "CHUBB": "new"}


def test_telegram_split():
    from radar.common import _split
    parts = _split("\n\n".join(["x" * 1000] * 10))
    assert all(len(p) <= 3900 for p in parts) and len(parts) >= 3


def test_wikifolio_kpi_by_label():
    from radar.wikifolio import _kpi
    kf = {"topKpis": [{"ranking": {"label": "Performance (1 J)", "value": 0.045}},
                      {"ranking": {"label": "Volatilität (1 J)", "value": None}}],
          "kpis": [{"rankings": [{"ranking": {"label": "Volatilität (Max)", "value": 0.227}}]}]}
    assert round(_kpi(kf, "Performance (1 J"), 1) == 4.5
    assert round(_kpi(kf, "Volatilität (1 J", "Volatilität (Max"), 1) == 22.7
    assert _kpi(kf, "gibt es nicht") is None


def test_etoro_formats_from_live_api():
    from radar.etoro import has_stop_loss, profile_from_tradeinfo
    # Werte wie live von /tradeinfo: Prozent, nicht Bruch
    p = profile_from_tradeinfo({"riskScore": 4, "peakToValley": -10.16, "trades": 120,
                                "highLeveragePct": 0.5, "winRatio": 87.13, "gain": 0.8})
    assert p.high_leverage_pct == 0.5 and p.max_drawdown_pct == 10.16 and p.win_ratio_pct == 87.13
    assert p.gain_1y_pct == 0.8 and p.trades_per_month == 10
    assert profile_from_tradeinfo({"gain": 56.77}, gain_fraction=0.5677).gain_1y_pct == pytest.approx(56.77)
    # Platzhalter für "kein Stop-Loss"
    assert not has_stop_loss(0.0001, 12.58, short=False)
    assert not has_stop_loss(230.0, 105.2, short=True)
    assert has_stop_loss(160.0, 180.0, short=False)
    assert has_stop_loss(115.0, 105.2, short=True)


def test_wikifolio_trades_paging(monkeypatch):
    from radar import wikifolio as w

    class R:
        ok, status_code, text = True, 200, ""

        def __init__(self, page):
            orders = [{"id": f"{page}-{i}", "orderType": "BuyLimit" if i else None} for i in range(20)]
            self.data = {"tradeHistory": {"pageCount": 3, "orders": orders}}

        def json(self):
            return self.data

        def raise_for_status(self):
            pass

    calls = []

    def fake_get(s, url, params=None, **kw):
        assert params["pagesize"] <= 20
        calls.append(params["page"])
        return R(params["page"])

    monkeypatch.setattr(w, "get", fake_get)
    monkeypatch.setattr(w.time, "sleep", lambda s: None)
    api = w.Wikifolio.__new__(w.Wikifolio)
    api.s = None
    out = api.trades("x", 100)
    assert calls == [0, 1, 2]  # stoppt bei pageCount
    assert len(out) == 57 and all(o["orderType"] for o in out)  # Dividenden/Splits raus
    assert len(api.trades("x", 20)) == 20


def test_http_error_never_logs_body():
    from radar.common import http_error

    class R:
        def __init__(self, code, body, is_json):
            self.status_code, self.text, self._b, self._j = code, str(body), body, is_json

        def json(self):
            if not self._j:
                raise ValueError
            return self._b

    assert http_error(R(422, {"errorCode": "InvalidKey", "errorMessage": "Key is invalid"}, True)) \
        == "HTTP 422 (InvalidKey Key is invalid)"
    assert http_error(R(400, {"message": "Page size cannot be bigger than 20."}, True)) \
        == "HTTP 400 (Page size cannot be bigger than 20.)"
    html = R(500, "<html>Angemeldet als max.mustermann@example.com</html>", False)
    assert "mustermann" not in http_error(html) and http_error(html).startswith("HTTP 500")
    assert "Depot" not in http_error(R(403, {"user": {"name": "Depot X"}}, True))


def _wf_pos(name, qty, weight, price, isin=None):
    return {"name": name, "isin": isin or name, "qty": qty, "weight": weight, "price": price,
            "type": "stock", "lev": False}


def test_wikifolio_parse_portfolio_all_groups():
    from radar.wikifolio import parse_portfolio
    d = {"totalValue": 1000.0, "groups": [
        {"type": 620, "items": [{"name": "Berkshire", "isin": "US1", "quantity": 2.0, "percentage": 0.2, "mid": 100}]},
        {"type": 630, "items": [{"name": "iShares", "isin": "IE1", "quantity": 10.0, "percentage": 0.1, "close": 10}]},
        {"type": 0, "items": []}]}
    pos, total = parse_portfolio(d)
    assert total == 1000 and set(pos) == {"US1", "IE1"}
    assert pos["US1"]["weight"] == 20 and pos["IE1"]["type"] == "etf" and pos["IE1"]["price"] == 10


def test_wikifolio_diff_uses_quantity():
    from radar.wikifolio import diff_portfolio
    prev = {"A": _wf_pos("A", 100, 10.0, 10), "B": _wf_pos("B", 50, 5.0, 10),
            "C": _wf_pos("C", 10, 2.0, 20), "D": _wf_pos("D", 5, 0.5, 10)}
    cur = {"A": _wf_pos("A", 100, 14.0, 14),   # nur Kurs gestiegen -> keine Meldung
           "B": _wf_pos("B", 80, 8.0, 10),     # +30 Stück * 10 / 10.000 = 3 Pp -> aufgestockt
           "C": _wf_pos("C", 9, 1.8, 20),      # -1 Stück = 0,2 Pp -> zu klein
           "N": _wf_pos("N", 20, 2.0, 10),     # neu, über Schwelle
           "M": _wf_pos("M", 1, 0.3, 10)}      # neu, unter Schwelle
    kinds = {c["pos"]["name"]: c["kind"] for c in diff_portfolio(prev, cur, 10_000, 1.0, 1.5)}
    assert kinds == {"B": "add", "N": "new"}  # D (0,5 %) verkauft, aber unter min_weight
    prev["D"]["weight"] = 3.0
    kinds = {c["pos"]["name"]: c["kind"] for c in diff_portfolio(prev, cur, 10_000, 1.0, 1.5)}
    assert kinds["D"] == "exit"
    # ohne Stückzahl: Gewicht vergleichen
    p2, c2 = {"X": _wf_pos("X", None, 6.0, None)}, {"X": _wf_pos("X", None, 3.0, None)}
    assert diff_portfolio(p2, c2, None, 1.0, 1.5)[0]["kind"] == "reduce"


def test_wikifolio_run_portfolio_flow(monkeypatch, capsys):
    from radar import wikifolio as w
    nd = {"props": {"pageProps": {"data": {"wikifolio": {"id": "wid", "title": "Testdepot"}, "keyFigures": {}}}}}
    depots = [
        ({"US1": _wf_pos("Alpha AG", 100, 10.0, 10, "US1"), "US2": _wf_pos("Beta AG", 50, 5.0, 10, "US2")}, 10_000),
        ({"US1": _wf_pos("Alpha AG", 300, 30.0, 10, "US1"), "US3": _wf_pos("Gamma AG", 40, 4.0, 10, "US3")}, 10_000),
    ]

    class Fake:
        def __init__(self):
            pass

        def page_data(self, name):
            return nd

        def trades(self, wf_id, count=20):
            return [{"isin": "US1", "orderType": "BuyLimit", "executionPrice": 10.5,
                     "executionDate": "2026-10-30T10:00:00+01:00"}]

        def portfolio(self, name):
            return depots.pop(0)

    monkeypatch.setattr(w, "Wikifolio", Fake)
    monkeypatch.setattr(w.time, "sleep", lambda s: None)
    cfg = {"wikifolios": ["wftest"], "min_weight_pct": 1.0, "min_change_pct": 1.5}
    w.run(cfg, {})
    assert "TELEGRAM" not in capsys.readouterr().out  # erster Lauf: nur Ausgangszustand
    w.run(cfg, {})
    out = capsys.readouterr().out
    assert "AUFGESTOCKT: <b>Alpha AG</b>" in out and "Anteil 10,0 % → 30,0 %" in out and "Stück 100 → 300" in out
    assert "Letzter Trade: Kauf · 30.10. 09:00 UTC · Kurs 10,5" in out
    assert "NEU IM DEPOT: <b>Gamma AG</b>" in out
    assert "KOMPLETT VERKAUFT: <b>Beta AG</b>" in out and "Stück 50 → 0" in out
    assert "Testdepot" in out and "Trade-Risiko" in out


# ---------------------------------------------------------------- eToro: Teilpositionen zusammenfassen
def _pos(pid, iid, pct, rate, ts="2026-10-30T14:46:00Z", profit=None, buy=True, lev=1):
    return {"positionId": pid, "instrumentId": iid, "isBuy": buy, "leverage": lev, "investmentPct": pct,
            "openRate": rate, "netProfit": profit, "stopLossRate": 0, "openTimestamp": ts}


def _etoro_two_runs(monkeypatch, capsys, before, after, now="2026-10-30T15:05:00Z", min_pct=0.5):
    from datetime import datetime
    from radar import etoro
    monkeypatch.setenv("ETORO_API_KEY", "x")
    pf = {"positions": before, "socialTrades": []}

    class Fake:
        def rankings(self, *a):
            return []

        def live_portfolio(self, u):
            return pf

        def trade_info(self, u, period="OneYearAgo"):
            return {"riskScore": 4, "trades": 60}

        def instruments(self, ids):
            names = {1002: ("Alphabet", "GOOG"), 1003: ("Coherent", "COHR"), 1004: ("Nvidia", "NVDA"),
                     1005: ("Tesla", "TSLA"), 1006: ("Amazon", "AMZN"), 1007: ("Meta", "META")}
            return {i: {"name": names[i][0], "symbol": names[i][1], "type": "stock"} for i in ids if i in names}

    monkeypatch.setattr(etoro, "EToro", Fake)
    monkeypatch.setattr(etoro.time, "sleep", lambda s: None)
    monkeypatch.setattr(etoro, "_now", lambda: datetime.fromisoformat(now.replace("Z", "+00:00")).timestamp())
    cfg = {"traders": ["choose3395"], "min_position_pct": min_pct}
    etoro.run(cfg, {})
    capsys.readouterr()
    pf["positions"] = after
    etoro.run(cfg, {})
    out = capsys.readouterr().out
    print(out)
    return out


def _goog5():
    return [_pos(10 + i, 1002, 2.0, 160 + i, ts=f"2026-09-0{i + 1}T10:00:00Z", profit=10.0 + i) for i in range(5)]


def test_etoro_umgeschichtet_choose3395(monkeypatch, capsys):
    """5x GOOG geschlossen, 1x GOOG neu -> eine Meldung statt sechs."""
    out = _etoro_two_runs(monkeypatch, capsys, _goog5(), [_pos(20, 1002, 3.0, 175.5, ts="2026-10-30T14:46:00Z")])
    assert out.count("TELEGRAM") == 1
    assert "UMGESCHICHTET: <b>Alphabet</b> (GOOG)" in out
    assert "5 Positionen geschlossen: 10,0 % des Depots · Ø-Ergebnis +12,0 %" in out
    assert "1 Position neu eröffnet: Kurs 175,5 · ohne Hebel · 3,0 % des Depots" in out
    assert "Eröffnet: 30.10. 14:46 UTC" in out and "Verzögerung" not in out


def test_etoro_nur_geschlossen(monkeypatch, capsys):
    out = _etoro_two_runs(monkeypatch, capsys, _goog5(), [])
    assert out.count("TELEGRAM") == 1
    assert "VERKAUFT / GESCHLOSSEN: <b>Alphabet</b>" in out
    assert "5 Positionen · 10,0 % des Depots · Ø-Ergebnis +12,0 %" in out


def test_etoro_teilweise_geschlossen_gewichtet(monkeypatch, capsys):
    before = _goog5()
    before[0]["investmentPct"] = 6.0  # Ø-Ergebnis gewichtet: (6*10 + 2*11) / 8 = 10,25
    out = _etoro_two_runs(monkeypatch, capsys, before, before[2:])
    assert out.count("TELEGRAM") == 1
    assert "TEILWEISE VERKAUFT" in out and "2 Positionen · 8,0 % des Depots · Ø-Ergebnis +10,2 %" in out
    assert "Noch im Depot: 3 Positionen" in out


def test_etoro_nur_geoeffnet_mit_spanne_und_verzoegerung(monkeypatch, capsys):
    # Coherent: zwei Teilkäufe -> eine Meldung; Schwelle gilt für die Summe (0,3 + 0,3 >= 0,5)
    new = [_pos(30, 1003, 0.3, 100, ts="2026-10-30T14:46:00Z"), _pos(31, 1003, 0.3, 300, ts="2026-10-30T15:00:00Z")]
    out = _etoro_two_runs(monkeypatch, capsys, [], new, now="2026-10-30T15:45:00Z")
    assert out.count("TELEGRAM") == 1
    assert "NEU GEKAUFT: <b>Coherent</b>" in out
    assert "Ø-Kurs 150 · ohne Hebel · 0,6 % des Depots · 2 Positionen" in out  # 0,6 / (0,3/100 + 0,3/300)
    assert "Eröffnet: 30.10. 14:46–15:00 UTC" in out
    assert "Gemeldet mit Verzögerung: ca. 45 Min." in out


def test_etoro_nachgekauft_risiko_summe(monkeypatch, capsys):
    old = [_pos(40, 1003, 4.0, 90, ts="2026-10-01T10:00:00Z")]
    new = old + [_pos(41, 1003, 6.0, 100, lev=2), _pos(42, 1003, 5.0, 101, buy=False)]
    out = _etoro_two_runs(monkeypatch, capsys, old, new)
    assert out.count("TELEGRAM") == 1
    assert "SHORT ERÖFFNET" in out  # eine der neuen Positionen ist short
    assert "Hebel x2" in out and "große Position (11,0 %)" in out and "Leerverkauf" in out


def test_etoro_unter_schwelle_ignoriert(monkeypatch, capsys):
    out = _etoro_two_runs(monkeypatch, capsys, [], [_pos(50, 1003, 0.2, 100), _pos(51, 1003, 0.2, 101)])
    assert "TELEGRAM" not in out


def test_etoro_sammelmeldung(monkeypatch, capsys):
    before = _goog5()
    after = [_pos(20, 1002, 3.0, 175.5)] + [_pos(60 + i, 1003 + i, 2.0, 50 + i) for i in range(5)]
    out = _etoro_two_runs(monkeypatch, capsys, before, after)
    assert out.count("TELEGRAM") == 1 and out.count("👤") == 1
    assert "6 Instrumente geändert" in out
    for name in ("Alphabet", "Coherent", "Nvidia", "Tesla", "Amazon", "Meta"):
        assert name in out
    assert "UMGESCHICHTET: <b>Alphabet</b> (GOOG) · −10,0 % / +3,0 % · 6 Pos." in out
