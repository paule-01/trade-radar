"""Gemeinsame Hilfsfunktionen: Konfiguration, Zustand, HTTP, Telegram."""
from __future__ import annotations

import html
import json
import logging
import os
import time
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent


def env(name: str, default: str = "") -> str:
    """Umgebungsvariable/Secret ohne Leerzeichen und Zeilenumbrüche am Rand (Kopierfehler)."""
    return os.environ.get(name, default).strip()


STATE_DIR = Path(env("RADAR_STATE_DIR") or ROOT / "state")
DRY_RUN = env("DRY_RUN").lower() in ("1", "true", "yes")

log = logging.getLogger("radar")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# SEC verlangt einen User-Agent mit Kontakt; wird über Secret/Env gesetzt.
CONTACT = env("CONTACT_EMAIL") or "trade-radar@example.com"
UA = f"TradeRadar/1.0 ({CONTACT})"


FAILURES: list[str] = []


def fail(source: str, err) -> None:
    """Fehler einer Quelle merken (wird in der Tageszusammenfassung angezeigt)."""
    log.error("%s: %s", source, err)
    if source not in FAILURES:
        FAILURES.append(source)


def load_config() -> dict:
    with open(ROOT / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


# ---------------------------------------------------------------- Zustand
def load_state(name: str) -> dict:
    p = STATE_DIR / f"{name}.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # kaputte Datei -> neu anfangen
            log.warning("State %s unlesbar, starte neu", name)
    return {}


def save_state(name: str, data: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    p = STATE_DIR / f"{name}.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(p)


# ---------------------------------------------------------------- HTTP
def session(extra_headers: dict | None = None) -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Encoding": "gzip, deflate"})
    if extra_headers:
        s.headers.update(extra_headers)
    return s


def http_error(r: requests.Response) -> str:
    """Statuscode plus kurzes Fehlerfeld aus JSON, nie die ganze Antwort.

    Die Actions-Protokolle öffentlicher Repos sind für alle lesbar; HTML-Fehlerseiten einer
    angemeldeten Sitzung können Kontonamen enthalten.
    """
    msg = None
    try:
        d = r.json()
        if isinstance(d, dict):
            msg = " ".join(str(d[k]) for k in ("errorCode", "errorMessage", "message", "error", "description")
                           if d.get(k) and not isinstance(d[k], (dict, list)))
    except ValueError:
        msg = "keine JSON-Antwort"
    return f"HTTP {r.status_code}" + (f" ({msg[:120]})" if msg else "")


def get(s: requests.Session, url: str, retries: int = 3, **kw) -> requests.Response:
    """GET mit Wiederholung bei 429/5xx und Beachtung von Retry-After."""
    kw.setdefault("timeout", 30)
    last = None
    for attempt in range(retries):
        try:
            r = s.get(url, **kw)
        except requests.RequestException as e:
            last = e
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code == 429 or r.status_code >= 500:
            wait = int(r.headers.get("Retry-After", 5 * (attempt + 1)))
            log.info("HTTP %s bei %s – warte %ss", r.status_code, url, wait)
            time.sleep(min(wait, 60))
            last = r
            continue
        return r
    if isinstance(last, requests.Response):
        return last
    raise last  # type: ignore[misc]


# ---------------------------------------------------------------- Telegram
TG_LIMIT = 3900


def esc(x) -> str:
    return html.escape(str(x if x is not None else ""), quote=False)


def send_telegram(text: str, silent: bool = False) -> None:
    """Schickt eine HTML-formatierte Nachricht; teilt zu lange Nachrichten auf.

    Bricht mit Fehler ab, wenn Token/Chat-Id fehlen oder Telegram ablehnt (außer bei DRY_RUN),
    damit der GitHub-Lauf rot wird statt still grün.
    """
    chunks = _split(text)
    if DRY_RUN:
        for c in chunks:
            print("\n----- TELEGRAM (Dry-Run) -----\n" + c)
        return
    token, chat = env("TELEGRAM_BOT_TOKEN"), env("TELEGRAM_CHAT_ID")
    if not token or not chat:
        raise RuntimeError("TELEGRAM_BOT_TOKEN oder TELEGRAM_CHAT_ID fehlt (für Probeläufe DRY_RUN=1 setzen)")
    for c in chunks:
        payload = {"chat_id": chat, "text": c, "parse_mode": "HTML",
                   "disable_web_page_preview": True, "disable_notification": silent}
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        try:
            r = requests.post(url, json=payload, timeout=30)
            if r.status_code == 429:
                time.sleep(int(r.json().get("parameters", {}).get("retry_after", 5)))
                r = requests.post(url, json=payload, timeout=30)
        except requests.RequestException as e:
            # Fehlertext enthielte die URL mit dem Bot-Token
            raise RuntimeError(f"Telegram nicht erreichbar: {type(e).__name__}") from None
        if not r.ok:
            raise RuntimeError(f"Telegram-Fehler {http_error(r)}")
        time.sleep(0.5)


def _split(text: str) -> list[str]:
    if len(text) <= TG_LIMIT:
        return [text]
    out, cur = [], ""
    for block in text.split("\n\n"):
        if len(cur) + len(block) + 2 > TG_LIMIT and cur:
            out.append(cur)
            cur = ""
        while len(block) > TG_LIMIT:  # Notbremse für riesige Blöcke
            out.append(block[:TG_LIMIT])
            block = block[TG_LIMIT:]
        cur = f"{cur}\n\n{block}" if cur else block
    if cur:
        out.append(cur)
    return out


# ---------------------------------------------------------------- Format
def fmt_money(v: float | None, cur: str = "$") -> str:
    if v is None:
        return "?"
    a = abs(v)
    if a >= 1e9:
        s = f"{a/1e9:.1f} Mrd"
    elif a >= 1e6:
        s = f"{a/1e6:.1f} Mio"
    elif a >= 1e3:
        s = f"{a/1e3:.0f} Tsd"
    else:
        s = f"{a:.0f}"
    return f"{'-' if v < 0 else ''}{s} {cur}".replace(".", ",")


def fmt_pct(v: float | None, digits: int = 1, sign: bool = False) -> str:
    if v is None:
        return "?"
    s = f"{v:+.{digits}f}" if sign else f"{v:.{digits}f}"
    return s.replace(".", ",") + " %"


def to_float(x) -> float | None:
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    s = str(x).strip().replace("$", "").replace("€", "").replace("%", "").replace("+", "")
    s = s.replace("\xa0", "").replace(" ", "")
    if not s or s in ("-", "n/a"):
        return None
    # deutsches Format 1.234,56 vs. englisches 1,234.56
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        parts = s.split(",")
        s = s.replace(",", "") if len(parts[-1]) == 3 and len(parts) > 1 else s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None
