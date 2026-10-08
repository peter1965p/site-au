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

8. **PageSpeed zog die besten Leads nach unten.** Nachdem der `PSI_KEY` da war
   und `--pagespeed` erstmals echt lief, zeigte sich: PSI misst Geschwindigkeit,
   nicht Zeitgemaessheit. Alte Baukastenseiten sind klein und schnell, moderne
   Seiten mit viel Bildmaterial langsam - ueber die fuenf Testseiten korreliert
   der PSI-Wert mit **+0,82** zum Chaos-Score. Mit dem anfaenglichen Gewicht 10
   schrumpfte der Abstand Info-Terra zu Frese & Salbert von Faktor 14 auf 4,7.
   Gewicht daher auf 5 gesenkt (Faktor 6,9), PSI bleibt als Argumentquelle drin.

9. **url_key liess das Schema stehen.** Steht derselbe Lead einmal als
   `firma.de` und einmal als `https://firma.de` in der CSV, entstanden zwei
   getrennte Cache- und Status-Eintraege fuer dieselbe Firma. `url_key()`
   normalisiert das jetzt zentral fuer Cache und Statusbuch.

## Oberflaeche (gui.py)

PySide6 6.12, laeuft mit Python 3.14 (abi3-Wheels, **kein** Qt-Installer noetig).

- Tabelle mit Score-Ampel, Detailbereich, Akquise-Status unten
- `Schalter`: eigener Schiebeschalter, weil Qt keinen mitbringt. Erbt von
  QAbstractButton, laesst sich also wie eine QCheckBox ansprechen.
  `sofort_setzen()` uebernimmt ohne Animation - beim Durchklicken von Leads
  waere die Animation nur Flackern und sieht nach einer Nutzeraktion aus.
- Lauf laeuft in einem QThread. SQLite-Verbindungen gehoeren dem Thread, der
  sie oeffnet, deshalb wird der Cache **im** Thread erzeugt. DB im WAL-Modus,
  damit die GUI gleichzeitig lesen und schreiben kann.
- QWebEngineView wird erst beim ersten Blick auf die Vorschau erzeugt; sonst
  startet bei jedem Programmstart eine Chromium-Instanz, die oft nie gebraucht
  wird (Fenster ist so nach 0,35 s da). Fehlt QtWebEngine, erklaert der Tab
  sich selbst statt zu kippen.
- Die Vorschau ist zwangslaeufig Chromium. Qt kann Firefox nicht einbetten,
  Mozilla hat die Einbettungs-API fuer Gecko eingestellt. Doppelklick und
  "Im Browser öffnen" gehen deshalb in den Standardbrowser des Systems.
- Tests laufen offscreen ueber `QT_QPA_PLATFORM=offscreen`, ohne Netz.

## Start aus dem Anwendungsmenue

`./install-desktop.sh` erzeugt den Eintrag aus `desktop/site-audit.desktop.in`.
Drei Dinge, die dabei leicht schiefgehen:

- `app.setDesktopFileName(APP_ID)` muss zum Dateinamen der .desktop passen,
  sonst ordnet Wayland das Fenster dem Eintrag nicht zu und die Taskleiste
  zeigt ein generisches Icon.
- Beim Start aus dem Menue gibt es kein sinnvolles Arbeitsverzeichnis.
  Datenbank und `.env` werden deshalb relativ zu `gui.py` gesucht.
- Nur **eine** Hauptkategorie in `Categories`, sonst erscheint die App
  mehrfach im Menue (`desktop-file-validate` warnt davor).

## Beim Bauen gemachte Fehler, die nicht wieder passieren duerfen

- Fuer einen Screenshot wurden erfundene Akquise-Notizen in die **echte**
  Datenbank geschrieben ("Termin 15.10. 14 Uhr vor Ort"). Solche Daten sind
  von echten nicht zu unterscheiden und koennen zu einer Fahrt zu einem
  Termin fuehren, den es nie gab. Beispieldaten gehoeren in eine temporaere
  Datei, niemals in `.site-audit-cache.sqlite`.
- Die GUI-Tests griffen auf die echten QSettings zu. Dadurch stellte ein Test
  die zuletzt geoeffnete Liste wieder her und startete einen **echten
  Netzlauf**. Jetzt werden QSettings per Fixture auf einen Wegwerfpfad
  umgebogen und vor jedem Test geleert.

## Moegliche naechste Schritte

- `--pagespeed` ist verifiziert (Key liegt in `.env`, nicht im Repo)
- Groessere Lead-Listen: `--assets 5` setzen, das dominiert die Laufzeit
- PSI kostet rund 15 s pro Seite, besser nur auf die Top-Kandidaten anwenden
