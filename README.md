# TR Stock Dashboard — Installationsanleitung

## Schnellstart (nach einmaliger Ersteinrichtung)

Sobald Schritt 1–2 unten einmal gemacht wurden: einfach **`start.bat`**
(Windows) bzw. `./start.sh` (macOS/Linux) doppelklicken/ausführen. Das Skript
aktiviert automatisch die venv, installiert fehlende Pakete nach und startet
die App — kein Terminal-Getippe mehr nötig.

## 1. Voraussetzungen
- Python 3.10 oder neuer installiert (`python3 --version`)

## 2. Setup (einmalig)

```bash
# Projektordner betreten (dort wo app.py, requirements.txt liegen)
cd tr-stock-dashboard

# Virtuelle Umgebung anlegen
python3 -m venv venv

# Aktivieren
source venv/bin/activate        # macOS / Linux
venv\Scripts\activate           # Windows (PowerShell/cmd)

# Abhängigkeiten installieren
pip install -r requirements.txt
```

## 3. Starten

```bash
streamlit run app.py
```

Der Browser öffnet sich automatisch unter `http://localhost:8501`.

## 4. Optional: Alpaca-Live-Daten aktivieren

Standardmäßig holt die App Live-Preise kostenlos und ohne Anmeldung über
`yfinance` (funktioniert für US- **und** europäische Ticker, z.B. `SAP.DE`).

Wer zusätzlich die etwas direktere Alpaca-IEX-Schnittstelle für US-Ticker
nutzen möchte:

1. Kostenloses Konto auf https://alpaca.markets anlegen (Paper-Trading-Konto reicht).
2. API Key ID + Secret Key erzeugen.
3. In der App links unter „⚙️ Alpaca Markets API (optional)" eintragen.

Die Keys werden nur im Browser-Session-Speicher gehalten, nicht auf der
Festplatte gespeichert.

## 5. Trading-Strategien (Bull Flag / Warrior Trading)

Die App enthält ein Strategie-Plugin-System (Dropdown links unter "Strategie").
Aktuell implementiert: **Bull Flag (Warrior Trading Style)**, angelehnt an das
von Ross Cameron beschriebene Muster (Flaggenmast → Konsolidierung mit
abnehmendem Volumen → Ausbruch).

- **Analyse-Seite**: Bei Zeitraum "1 Tag" oder "5 Tage" wird das Muster
  automatisch gesucht und Entry, Stop-Loss, Kursziel sowie Chance-Risiko
  direkt im Chart eingezeichnet (grün = Entry, rot = Stop, blau = Ziel).
- **Scanner-Seite**: prüft deine gesamte Watchlist auf Gap %, Relativvolumen
  und das Bull-Flag-Muster gleichzeitig.

**Wichtige Einschränkung:** Das ist eine regelbasierte Annäherung an ein
Muster, das Ross Cameron live mit Level-2-Orderbuchdaten liest. Es ist
**keine Anlageberatung** und keine Garantie für ein gültiges Setup — nutze es
als Ausgangspunkt, nicht als automatisches Handelssignal. Außerdem: Ross'
klassische Kandidaten sind extreme Micro-Caps mit sehr kleinem Float, die bei
Trade Republic oft gar nicht handelbar sind; der Scanner durchsucht bewusst
nur deine eigene, kostenlos abrufbare Watchlist und nicht den Gesamtmarkt
(ein echter Markt-weiter Scan bräuchte einen kostenpflichtigen Profi-Feed).

Die Bull-Flag-Parameter (Mindestanstieg, Flaggenlänge, max. Retracement)
lassen sich links im Expander "⚙️ Bull-Flag-Parameter" live anpassen.

## 7. Als echte Webapp mit eigener URL (Streamlit Community Cloud, kostenlos)

Das lokale `streamlit run app.py` startet nur einen Server auf deinem
eigenen PC (`localhost`) — der läuft, solange dein Terminal offen ist, und
ist nur von deinem Rechner aus erreichbar. Für eine "echte" Webapp mit
eigener URL, die du von jedem Gerät/Browser aus aufrufen kannst, ohne dein
Terminal zu benutzen, ist **Streamlit Community Cloud** der einfachste
kostenlose Weg:

1. Kostenlosen GitHub-Account anlegen (falls noch nicht vorhanden) und ein
   neues, **privates** Repository erstellen.
2. `app.py`, `requirements.txt` und `README.md` in dieses Repo pushen
   (`watchlist.json` NICHT einchecken — die legt die App bei Bedarf selbst an).
3. Auf https://share.streamlit.io mit dem GitHub-Account einloggen.
4. "New app" → das Repository auswählen → `app.py` als Hauptdatei angeben → Deploy.
5. Nach ca. 1–2 Minuten bekommst du eine URL wie
   `https://dein-name-tr-dashboard.streamlit.app` — die kannst du dir als
   Lesezeichen speichern oder auf dem Handy öffnen.
6. Optional, für Alpaca-Keys ohne manuelle Eingabe: in der Streamlit-Cloud-
   App unter "Settings → Secrets" einfügen:
   ```toml
   ALPACA_API_KEY = "dein-key"
   ALPACA_SECRET_KEY = "dein-secret"
   ```
   Die App liest diese automatisch (siehe `st.secrets` in `app.py`).

**Zwei ehrliche Hinweise dazu:**
- Da die Anfragen dann von Streamlit Clouds Servern kommen, nicht von
  deinem PC, betrifft dich das lokale AdGuard/`fc.yahoo.com`-Problem dort
  in der Regel nicht mehr.
- `watchlist.json` liegt auf einem Cloud-Dateisystem, das bei einem
  Redeploy oder nach längerer Inaktivität zurückgesetzt werden kann. Für
  eine wirklich dauerhafte Watchlist müsste man sie z. B. in eine externe
  Datenbank auslagern — für den persönlichen Gebrauch reicht der aktuelle
  Ansatz meist aus, aber es ist kein Garant für ewige Persistenz wie bei
  einer lokalen Datei auf deinem eigenen PC.

## 8. Watchlist (lokal)

Gespeicherte Aktien liegen lokal in `watchlist.json` im selben Ordner wie
`app.py` und bleiben nach Neustart erhalten.

## 9. Troubleshooting

| Problem | Lösung |
|---|---|
| `ModuleNotFoundError` | `pip install -r requirements.txt` erneut ausführen (venv aktiv?) |
| Keine Kursdaten für einen Ticker | Yahoo-Suffix prüfen (`.DE`, `.PA`, `.AS`, `.SW`, `.CO` …) |
| "Rate limited" / leere Charts | Aktualisierungsintervall im Slider erhöhen (Yahoo Finance ist inoffiziell und rate-limitet bei zu vielen Anfragen) |
| Auto-Refresh funktioniert nicht | `pip install streamlit-autorefresh` |
