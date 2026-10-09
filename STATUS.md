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

## Aenderungen vom 2026-10-09

1. **Slider Revolution nur mit Versionsnachweis.** Haus Anny laeuft auf 6.7.38 -
   das ist die aktuelle, gepflegte 6.x-Reihe (Plugin 6.7.40 vom Dez. 2025, die
   "SR7 Velocity Engine" steckt in der 6.7er-Reihe). Konstante
   `SLIDERREV_AKTUELLE_HAUPT = 6`; bei einer Plugin-7 anpassen.
2. **Bilder:** Lazy-Load-Platzhalter werden uebersprungen, die Breite kommt aus
   dem echten Bild (srcset/data-src/...). Nur Inhaltsbilder zaehlen, Argument
   erst ab 3. Nebenwirkung: Das Messbudget (15 Abrufe) wird nicht mehr an
   Platzhalter verschwendet, dadurch sehen Seitengewicht und Riesenbilder jetzt
   die echten Dateien (Pfahl: 2,1-MB-Bild, vorher unsichtbar).
3. **Status `nicht erreichbar`:** kein Score, Sortierung ans Ende, nicht im
   Scorefilter. Empfehlung immer "manuell ansehen" (frueher bei vorhandener
   Nummer "Anruf" - das war riskant, weil eine tote Adresse oft ein Tippfehler
   der Quelle ist). Altbestand wird beim Lesen umgestellt (`Result.__post_init__`).
4. **Aufgeraeumt:** `.site-audit-cache.sqlite-wal/-shm` waren versehentlich im
   Git (`*.sqlite` greift nicht auf die Hilfsdateien); aus dem Index genommen
   und ignoriert. Es gibt keinen Remote, nichts ist abgeflossen, sie stehen
   aber noch in der Historie (Commits c084da8, 349e9da).

Fuenf Testseiten danach: Info-Terra 65 (vorher 69), Pfahl 43 (40), Haus Anny 34
(47), Sonneck 24 (18), Frese 5 (5). Zu beachten: `leads.csv` ist inzwischen die
Arbeitsliste der GUI (Ergebnis der Firmensuche) und nicht mehr die Fuenf-
Seiten-Probe - fuer Tests eigene CSVs verwenden und `leads.csv` nicht
committen (enthaelt Telefonnummern und Mailadressen aus OpenStreetMap).

## Tempo (2026-10-09)

Der Lauf ueber ~17.700 Firmen schaffte 35/min (~7 h Rest). Messung an 40/240
zufaelligen Firmen: 19,7 s pro Firma, davon 54 % Warten im 1-s-Takt, der
groesste Posten waren bis zu 15 nacheinander abgerufene Dateien. Jetzt
184/min (~80 min fuer 14.600 Firmen):

- Parallelitaet 10 -> 30 (**weicht von der urspruenglichen Vorgabe "max 10"
  ab**; der Takt pro Host bleibt 1/s). In der GUI einstellbar (unten rechts).
- Assets 15 -> 6, nebenlaeufig angestossen. Kosten: Seitengewicht ist eine
  Untergrenze, ~0,8 Punkte mittlere Abweichung, Status identisch.
- Tote Adressen: kein Wiederholen, kein http-Versuch, robots.txt merkt sich
  den Host (`Fetcher._tot`). Connect-Timeout 8 s, Timeouts nur 1x wiederholt.
- Impressum: `status_und_text()` statt HEAD + GET.
- **Verbindungspool waechst mit der Parallelitaet.** Ist er zu klein, entsteht
  ein Pool-Timeout, der als "nicht erreichbar" gemeldet wuerde - ein
  stiller Fehler, der gute Seiten aus der Liste nimmt.
- `ssl_reason()` liest `verify_message` per `getattr` (nicht jede
  Zertifikats-Ausnahme hat das Attribut).

Aufgefallen: Das Telefonbuch (Das Oertliche) ist **nicht** der Engpass - nur
61 von 3045 Treffern kamen von dort.

## Absturz und Haenger im Suchdialog (2026-10-09)

Gemeldet: "Lade Kreise" hing minutenlang, danach brach das Programm ab
("QThread: Destroyed while thread '' is still running", Speicherabzug).

- **Absturz:** Die Suche lief in einem QThread. Wird der Dialog oder das Fenster
  geschlossen, waehrend die Netzabfrage haengt, zerstoert Qt das noch laufende
  Thread-Objekt und bricht das Programm ab. Jetzt ein Daemon-Thread
  (`im_hintergrund`), der beim Schliessen still mitstirbt. Regressionstest
  `test_dialog_schliessen_waehrend_die_abfrage_haengt_stuerzt_nicht_ab`.
  Gleiches Risiko beim Lauf-Thread: `closeEvent` wartet 20 s, danach
  `os._exit(0)` statt Speicherabzug (fertige Ergebnisse stehen per Commit je
  Firma laengst in der Datenbank).
- **Haenger:** `abfragen()` hatte keine Zeitgrenze (3 Runden x 2 Spiegel x 240 s
  plus Pausen = bis ueber 20 Minuten). Jetzt Zeitbudget (`max_sekunden`), und
  die Fehlermeldung nennt *alle* Server - vorher nur den zuletzt befragten,
  toten Spiegel, waehrend der Hauptserver das Problem war.
- **Kreise ohne Netz:** `kreise_de.json` liegt dem Projekt bei. Der Dialog liest
  sie sofort (0,7 ms statt eines Overpass-Aufrufs).
- Veraltete Antworten werden verworfen (Auftragsnummer), z. B. wenn das Land
  gewechselt wird, bevor die Kreise des ersten ankamen.
- Die JavaScript-Konsole der besuchten Seiten (Wix, Facebook) wird in der
  eingebetteten Vorschau verschluckt, sie hat die Konsole zugemuellt.

**Zwei Datenfehler der ersten Kreisliste, nur durch Gegenpruefen gefunden:**
Die Gesamtzahl 398 stimmte, verdeckte aber zwei sich aufhebende Fehler:
Hessen 27 statt 26 (Hanau, Sonderstatusstadt, kein Kreis) und Thueringen 21
statt 22 (Landkreis Greiz fehlte, weil OSM dort nur den amtlichen
Gemeindeschluessel fuehrt). Konsequenz auch fuer die Firmensuche: Ein
Gebietsfilter nur ueber den Regionalschluessel haette im Kreis Greiz stumm
nichts gefunden. `kreis_filter()` greift jetzt auf beide Tags zu (Test:
26 Handwerksbetriebe in Greiz). Die Datei wird gegen die bekannten
Kreiszahlen je Land geprueft, nicht nur gegen die Summe.

## Absturz "Firmensuche, dann Pruefen" (2026-10-09, 16:44)

Symptom: Nach der Firmensuche (Rheinland-Pfalz) und Klick auf "Pruefen" war das
Fenster weg. `coredumpctl info` zeigte SIGSEGV in
`Shiboken::BindingManager::runDeletionInMainThread` -> QtWidgets.

**Ursache.** Der Suchdialog bildet mit seinen Kindern (Schalter, Animationen,
Timer ...) Referenzzyklen, weil Signale mit eigenen Methoden verbunden sind. Er
wurde nach der Suche nicht abgebaut und blieb als zyklischer Muell liegen (15.068
Objekte, davon der ganze Widget-Baum). Beim Start von "Pruefen" legt der
Pruef-Thread zehntausende Objekte an, Pythons Speicherbereinigung laeuft **in
diesem Thread** und gibt den Dialog frei. Shiboken loescht Qt-Objekte dann
nachtraeglich im Hauptthread - die Widgets waren aber schon weg: doppelte
Loeschung, Absturz. Regel: **Qt-Objekte duerfen nie von einem anderen Thread aus
per Speicherbereinigung freigegeben werden.**

**Eingegrenzt per Experiment**, nicht per Vermutung: Absturz nachgestellt (gleiche
Spur, gleicher Offset `+0x252b19`); mit abgeschalteter Speicherbereinigung oder
einem `gc.collect()` im Hauptthread vor "Pruefen" kein Absturz;
`gc.DEBUG_SAVEALL` zeigte den Dialog samt Kindern als Muell.

**Korrektur (zweifach).**
1. `firmen_suchen()` baut den Dialog mit `deleteLater()` im Hauptthread ab, sobald die
   Treffer uebernommen sind.
2. `gc_im_hauptthread()`: automatische Speicherbereinigung aus, stattdessen alle 2 s
   `gc.collect(1)` und alle 60 s eine vollstaendige - aus dem Qt-Zeitgeber, also
   im Hauptthread. Faengt auch kuenftige Zyklen ab.
3. `faulthandler.enable()` in `main()`: bei einem Absturz steht die Python-Spur
   im Terminal statt nur "Speicherabzug geschrieben".

**Regressionstest** `tests/test_absturz.py` startet `szenario_suche_dann_pruefen.py`
als eigenen Prozess (ein Absturz wuerde sonst die Testsuite mitreissen). Ohne die
Korrektur stuerzt das Szenario in 6 von 6 Laeufen ab, mit ihr nie; ein
Wachposten-Test prueft, dass das so bleibt.

Daten waren nie in Gefahr: Der Lauf stuerzte ab, bevor er etwas Neues schrieb; die
4.557 Ergebnisse in der Datenbank und die 25.284 Firmen in `leads.csv` blieben
unversehrt.

## Moegliche naechste Schritte

- `--pagespeed` ist verifiziert (Key liegt in `.env`, nicht im Repo)
- Groessere Lead-Listen: `--assets 5` setzen, das dominiert die Laufzeit
- PSI kostet rund 15 s pro Seite, besser nur auf die Top-Kandidaten anwenden
