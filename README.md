# Trade-Radar

Schickt dir Telegram-Nachrichten, wenn bekannte Trader und Investoren kaufen oder verkaufen. Jede Meldung bekommt eine Risiko-Einordnung.

| Was | Wann | Quelle |
|---|---|---|
| **Sofort-Push:** eToro Popular Investors | alle ~10 Min. | offizielle eToro-API |
| **Sofort-Push:** wikifolio-Musterdepots | alle ~10 Min. | wikifolio-Webseite (inoffiziell) |
| **Tagesradar:** ARK Invest (Cathie Wood) | täglich ~7 Uhr | ARK-Bestandslisten |
| **Tagesradar:** Insider-Käufe USA | täglich | SEC Form 4 (über OpenInsider) |
| **Tagesradar:** Directors' Dealings DE | täglich | BaFin-Datenbank |
| **Tagesradar:** US-Politiker-Trades | täglich | Repräsentantenhaus + Senat |
| **Tagesradar:** große Fonds (Buffett & Co.) | wenn neu, ca. 4× pro Jahr | SEC 13F |

Läuft kostenlos auf **GitHub Actions**. Du brauchst keinen eigenen Server, und dein Computer muss nicht an sein.

---

## So liest du eine Sofort-Meldung

```
🔴 RISKANT · eToro · Gesamtrisiko 69/100
👤 alice (Alice A) – Profil: konservativ, regelmäßig aktiv
🆕 NEU GEKAUFT: Bitcoin (BTC) · Krypto
Kurs 70000 · Hebel x2 · 15,0 % des Depots
Trade-Risiko 100/100: Krypto, Hebel x2, große Position (15,0 %), kein Stop-Loss
Trader: eToro-Risiko 4/10 (max. 5) · max. Verlust 18 % · 5 Trades/Monat · Trefferquote 64 % · …
```

- **Ampel** (🟢 konservativ / 🟡 mittel / 🔴 riskant): Sie setzt sich zu 60 % aus dem Risiko dieses einen Trades und zu 40 % aus dem Profil des Traders zusammen.
- **Trade-Risiko** berücksichtigt die Anlageklasse (ETF < Aktie < Rohstoff < Devisen < Krypto < Hebelprodukt), den Hebel, Leerverkäufe, die Positionsgröße im Depot und einen fehlenden Stop-Loss.
- **Trader-Profil** (konservativ / ausgewogen / risikofreudig) ergibt sich aus dem eToro-Risikowert, dem größten Verlust in der Vergangenheit, dem Anteil an Hebel-Trades, der Handelshäufigkeit und der Länge der Historie.
- **Aktivität:** „Langfrist-Anleger, handelt selten“ (< 4 Trades/Monat), „regelmäßig aktiv“ oder „Vieltrader“ (> 20).

Die Schwellen für Gelb und Rot stellst du in `config.yaml` unter `risk:` ein.

---

## Einrichtung (ca. 20 Minuten, einmalig)

### 1. Telegram-Bot anlegen
1. Öffne in Telegram den Chat mit **@BotFather** und schreibe `/newbot`. Vergib einen Namen, z. B. „Mein Trade-Radar“.
2. Du bekommst einen **Token** (z. B. `7123456789:AAH…`). Das ist dein `TELEGRAM_BOT_TOKEN`.
3. Öffne deinen neuen Bot und drücke **Start**, damit er dir schreiben darf.
4. Öffne **@userinfobot**. Er zeigt dir deine **Id** (eine Zahl). Das ist deine `TELEGRAM_CHAT_ID`.

### 2. eToro-API-Schlüssel
1. eToro → **Einstellungen → Trading → API Key Management → Create New Key**
2. Umgebung **Real**, Berechtigung **nur „Read“** (Lesen genügt, der Radar kann so nie handeln).
3. Bestätige per SMS-Code und kopiere **API Key** (`ETORO_API_KEY`) und **User Key** (`ETORO_USER_KEY`).

### 3. wikifolio-Konto (kostenlos)
Depot und Handelshistorie sieht man auf wikifolio nur eingeloggt. Lege ein kostenloses Konto an. Zwei-Faktor-Login muss bei diesem Konto **aus** sein. Verwende am besten ein eigenes Passwort, das du nirgends sonst nutzt.

### 4. GitHub-Repository
1. Lege ein Konto auf github.com an. Erstelle dann über **New repository** z. B. `trade-radar` als **Public**. Warum öffentlich, steht unten bei den Kosten. Deine Schlüssel bleiben trotzdem geheim.
2. **Uploading an existing file** → alle Dateien aus diesem Ordner hineinziehen, **inklusive des versteckten Ordners `.github`**.
   - Mac: Drücke im Finder **Cmd + Shift + .**, dann wird `.github` sichtbar.
   - Windows: Im Explorer unter **Ansicht → Ausgeblendete Elemente** einschalten.
3. **Commit changes** klicken.

### 5. Geheimnisse hinterlegen
Repository → **Settings → Secrets and variables → Actions → New repository secret**. Lege diese Einträge an (Name genau so schreiben):

| Name | Inhalt |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Token vom BotFather |
| `TELEGRAM_CHAT_ID` | deine Id von @userinfobot |
| `ETORO_API_KEY` | eToro API Key |
| `ETORO_USER_KEY` | eToro User Key |
| `WIKIFOLIO_EMAIL` | Login-E-Mail wikifolio |
| `WIKIFOLIO_PASSWORD` | Passwort wikifolio |
| `CONTACT_EMAIL` | deine E-Mail (die US-Behörde SEC verlangt eine Kontaktadresse bei Abfragen) |

### 6. Starten und testen
1. Öffne den Reiter **Actions** und bestätige gegebenenfalls „I understand my workflows, go ahead and enable them“.
2. **Test-Nachricht an Telegram → Run workflow**. Danach sollte eine Beispiel-Meldung auf deinem Handy ankommen.
3. **Sofort-Push → Run workflow**. Der erste Lauf merkt sich nur den aktuellen Stand und meldet noch nichts. Ab dann kommen neue Trades.
4. **Tagesradar → Run workflow**. Auch hier ist der erste Lauf bei ARK, Politikern und Fonds ein reiner Ausgangsstand. Ab dem nächsten Tag kommen Änderungen.

Falls Schritt 4 mit „Permission denied“ beim Speichern scheitert: Gehe zu **Settings → Actions → General → Workflow permissions** und stelle auf **Read and write permissions**.

**Wenn etwas nicht klappt:**
- Ein Lauf wird **rot**, wenn `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` fehlen oder Telegram die Nachricht ablehnt. Den genauen Grund zeigt das Protokoll des Laufs.
- `eToro … HTTP 422: InvalidKey` heißt: eToro lehnt den **User Key** ab (z. B. für Demo statt Real erstellt, widerrufen, oder API Key und User Key stammen aus verschiedenen Erstellungen). Lege einen neuen Schlüssel an und ersetze **beide** Secrets.
- Leerzeichen oder Zeilenumbrüche am Anfang/Ende eines Secrets werden automatisch entfernt.

---

## Anpassen (`config.yaml`, direkt auf GitHub mit dem Stift-Symbol bearbeiten)

- **Eigene eToro-Trader:** Trage sie unter `etoro: traders:` mit Usernamen ein (der steht in der Profil-URL `etoro.com/people/USERNAME`).
- **Automatische Auswahl:** `auto_discover` holt die besten Popular Investors aus der eToro-Rangliste, standardmäßig die 15 besten mit Risiko ≤ 6 und mindestens 500 Kopierern. Die Liste wird wöchentlich erneuert.
- **wikifolios:** Trage die Kürzel unter `wikifolio: wikifolios:` ein, z. B. `wf00trend7`. Gemeldet wird nicht jede einzelne Order (große wikifolios handeln in vielen kleinen Teilorders), sondern was sich im Depot geändert hat: **neu aufgenommen** oder **komplett verkauft** ab `min_weight_pct` (Standard 1 % des Depots) und **aufgestockt/reduziert** ab `min_change_pct` (Standard 1,5 Prozentpunkte). Verglichen wird die Stückzahl, reine Kursbewegungen lösen also nichts aus.
- **Politiker filtern:** Unter `only_members:` kannst du z. B. `Pelosi` eintragen.
- **Schwellen:** Mindestbeträge für Insider-Käufe, die Mindeständerung bei ARK, Gelb/Rot usw.

## Kosten

- **Öffentliches Repository:** GitHub Actions ist komplett kostenlos und unbegrenzt. Öffentlich sichtbar sind nur der Code und die Liste der beobachteten Trader, nie deine Schlüssel.
- **Privates Repository:** 2.000 Freiminuten pro Monat. Alle 10 Minuten rund um die Uhr wären etwa 4.300 Minuten, das reicht also nicht. Stelle dann in `.github/workflows/sofort-push.yml` auf Börsenzeiten um, z. B. `cron: "*/15 7-21 * * 1-5"` (ca. 1.300 Minuten).
- eToro, Telegram und alle anderen Quellen sind kostenlos.

## Grenzen (wichtig)

- **Verzögerung:** GitHub startet Zeitpläne nicht sekundengenau. Rechne mit 5 bis 20 Minuten Verzögerung, in Stoßzeiten auch mehr. Für Meldungen innerhalb von 1 bis 2 Minuten bräuchte man einen kleinen Server für ca. 4 €/Monat. Der Code läuft dort unverändert, man ruft dann einfach `python -m radar.realtime` jede Minute auf.
- **wikifolio** ist inoffiziell angebunden. Baut wikifolio die Seite um, fällt nur dieser Teil aus. Bei einem Aktiensplit ändert sich die Stückzahl, das kann einmalig als „aufgestockt“ gemeldet werden.
- **Politiker-Trades** sind bis zu 45 Tage alt. Bei Papier-Meldungen gibt es nur den Link ohne Details.
- **13F-Fonds:** Das ist der Stand am Quartalsende, also bis zu 45 Tage alt.
- Die Risiko-Ampel ist eine **Faustregel aus öffentlichen Kennzahlen**, keine Garantie. Gute Trader können auch verlieren.

**Keine Anlageberatung.** Der Radar liefert Informationen. Entscheidungen triffst du selbst.

---

## Für Technik-Interessierte

```
radar/realtime.py   Sofort-Push (eToro, wikifolio)
radar/daily.py      Tagesradar (ARK, Insider, Kongress, 13F)
radar/risk.py       Risiko-Bewertung
state/              gemerkter Stand des Tagesradars (wird automatisch gespeichert)
tests/              Offline-Tests: python -m pytest -q
```
Probelauf ohne Telegram: `DRY_RUN=1 python -m radar.daily` bzw. `DRY_RUN=1 python -m radar.realtime` (die Nachrichten werden dann nur ausgegeben). Ohne `DRY_RUN` und ohne Telegram-Token bricht der Lauf mit Fehler ab.
