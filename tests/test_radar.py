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
    assert "VERKAUFT / GESCHLOSSEN" in out and "Apple" in out
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
    assert w._weight({"weightage": 0.0045}) == pytest.approx(0.45)
