"""Einstiegspunkt Tageszusammenfassung: ARK, Insider USA/DE, US-Politiker, 13F-Fonds."""
from datetime import datetime
from zoneinfo import ZoneInfo

from . import ark, congress, funds13f, insider
from . import common
from .common import load_config, log, send_telegram


def main() -> None:
    cfg = load_config().get("daily", {})
    parts = []
    jobs = [
        ("ARK", lambda: ark.section(cfg.get("ark", {}))),
        ("Insider USA", lambda: insider.us_section(cfg.get("insider_us", {}))),
        ("Insider DE", lambda: insider.de_section(cfg.get("insider_de", {}))),
        ("Kongress", lambda: congress.section(cfg.get("congress", {}))),
        ("13F", lambda: funds13f.section(cfg.get("funds_13f", {}))),
    ]
    failed = []
    for name, job in jobs:
        try:
            sec = job()
            if sec:
                parts.append(sec)
        except Exception:
            log.exception("%s fehlgeschlagen", name)
            failed.append(name)

    today = datetime.now(ZoneInfo("Europe/Berlin")).strftime("%d.%m.%Y")
    head = f"📊 <b>Tagesradar {today}</b>"
    if not parts:
        body = "Heute keine neuen Meldungen in deinen Quellen."
    else:
        body = "\n\n".join(parts)
    failed += [f for f in common.FAILURES if f not in failed]
    foot = f"\n\n⚠️ Nicht erreichbar: {', '.join(failed)}" if failed else ""
    foot += "\n\n<i>Nur zur Information, keine Anlageberatung.</i>"
    send_telegram(f"{head}\n\n{body}{foot}")


if __name__ == "__main__":
    main()
