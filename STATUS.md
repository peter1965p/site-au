# Arbeitsstand

Letzte Aktualisierung: 2026-10-08. Spezifikation: [SPEC.md](SPEC.md),
Bedienung: [README.md](README.md).

## Fertig

Alle Punkte der Vorgabe sind umgesetzt und abgenommen.

- [x] Setup: `app.py` geloescht, venv mit Python 3.14.7, Pakete installiert
- [x] `audit.py` mit allen 11 Checks, Chaos-Score, CLI, SQLite-Cache
- [x] `templates/report.html.j2` - dunkles Theme, sortier-/filterbar, Einzeldatei
- [x] `.env.example` mit `PSI_KEY=`
- [x] `leads.csv` mit den fuenf Abnahme-Seiten
- [x] Abnahmelauf: Info-Terra (Jimdo) **72,3** vs. Frese & Salbert **5,2**
- [x] 141 pytest-Tests gegen HTML-Fixtures, alle gruen
- [x] `README.md`

`requirements.txt` enthielt nie Flask - die sechs Zielpakete standen schon drin.

## Beim Bauen gefundene und behobene Probleme

Festgehalten, weil sie sich bei Aenderungen leicht wieder einschleichen:

1. **selectolax 1.0 hat das Modest-Backend abgeschaltet.**
   `from selectolax.parser import HTMLParser` wirft `ImportError`.
   Richtig: `from selectolax.lexbor import LexborHTMLParser`.
2. **Score-Skala war stumpf.** Mit der vollen Gewichtssumme als Nenner landete
   selbst eine ruinierte Seite nur bei ~45, die Farbschwellen griffen nie.
   Gelöst über `SCORE_REFERENZ = 75` oben in `audit.py`.
3. **Netzaussetzer kippten Leads.** Ein einzelner Verbindungsfehler machte aus
   einer gesunden Seite Score 100 - und schlimmer: bei einem Aussetzer auf
   https fiel das Tool auf http zurueck und meldete faelschlich "keine
   Verschluesselung". Jetzt `RETRIES = 3` mit Backoff pro Kandidaten-URL.
4. **Cookie-Banner wurden uebersehen.** Eine Liste bekannter Produktnamen
   reicht nicht (`cookie-law-info`, `ocn-cookie-wrap` fehlten). Jetzt erkennt
   `has_consent_tool()` die Spur generisch an eingebundenen Dateien und
   Container-IDs. Blosse Footer-Links zaehlen bewusst nicht.
5. **HTTP 529 als "Datenschutz-Link kaputt".** Ueberlastungsantworten, die wir
   uns mit den eigenen Asset-Requests selbst eingehandelt hatten. Jetzt gelten
   nur noch 403/404/410 als toter Link (`DEAD_LINK_CODES`).
6. **Telefon-Normalisierung** verschluckte die Null in `+49 (0)6591`.
7. **Konsistenz-Check lief ins Leere**, weil er im Roh-HTML suchte, das die
   Meta-Description selbst enthaelt - die gesuchte Nummer stand dadurch immer
   auch "im Text". Jetzt nur sichtbarer Text plus `tel:`-Links.

## Moegliche naechste Schritte

- Noch kein Commit im Repo (Branch `master`), falls gewuenscht
- `--pagespeed` ist gebaut, aber mangels `PSI_KEY` noch nie echt gelaufen
- Groessere Lead-Listen: `--assets 5` setzen, das dominiert die Laufzeit
