"""Schickt eine Beispiel-Nachricht, um den Telegram-Bot zu prüfen."""
from .common import send_telegram


def main() -> None:
    send_telegram(
        "✅ <b>Trade-Radar ist verbunden!</b>\n\n"
        "So sehen Sofort-Meldungen aus:\n\n"
        "🟡 <b>MITTEL</b> · eToro · Gesamtrisiko 48/100\n"
        "👤 <b>beispieltrader</b> – Profil: <b>ausgewogen</b>, regelmäßig aktiv\n"
        "🆕 NEU GEKAUFT: <b>NVIDIA</b> (NVDA) · Aktie\n"
        "Kurs 182,4 · ohne Hebel · 11,0 % des Depots\n"
        "Trade-Risiko 45/100: große Position (11,0 %)\n"
        "Trader: eToro-Risiko 5/10 · max. Verlust 24 % · 9 Trades/Monat · Trefferquote 61 %"
    )


if __name__ == "__main__":
    main()
