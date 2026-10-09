# site-audit

Prüft eine Liste von Firmenwebseiten automatisch auf Relaunch-Bedarf und
sortiert die schlechtesten nach oben. Gedacht für die Akquise: aus einer
Leads-CSV wird eine priorisierte Liste plus fertige Gesprächsargumente.

## Installation

```bash
cd ~/Dev/site-audit
python3 -m venv venv
./venv/bin/python -m pip install -r requirements.txt
```

## Oberfläche

```bash
./venv/bin/python gui.py leads.csv
```

### Ins Anwendungsmenü eintragen

```bash
./install-desktop.sh
```

Legt Menüeintrag und Icon unter `~/.local/share` an, ohne Root-Rechte. Danach
ist „site-audit“ im Anwendungsmenü zu finden und erscheint mit eigenem Icon in
der Taskleiste. Beim Start aus dem Menü gibt es keine Kommandozeile — die App
stellt deshalb die zuletzt geöffnete Liste und die Fenstergröße wieder her.
Datenbank und `.env` werden neben `gui.py` gesucht, nicht im Arbeitsverzeichnis,
damit das auch beim Start von irgendwoher funktioniert.

Entfernen:

```bash
rm ~/.local/share/applications/site-audit.desktop \
   ~/.local/share/icons/hicolor/scalable/apps/site-audit.svg
```

Liste links, Befunde und Website rechts, darunter der Akquise-Status. Das
Fenster startet mit dem, was schon im Cache liegt, „Prüfen" holt den Rest.
Doppelklick auf eine Zeile zeigt die Website im eingebetteten Browser.

Der Akquise-Status (offen, angerufen, Wiedervorlage, Termin, Angebot raus,
Kunde, kein Interesse, nicht passend), eine Notiz und ein Wiedervorlage-Datum
gehören zum Lead, nicht zum Prüfergebnis: `--refresh` wirft Befunde weg, die
Notiz aus einem Telefonat bleibt. Beides landet in derselben SQLite-Datei und
erscheint in `ergebnis.csv` sowie im HTML-Report.

„One-Pager als PDF" erzeugt eine Seite zur Gesprächsvorbereitung mit Score,
Argumenten, allen Befunden und Platz für handschriftliche Notizen.

Fällige Wiedervorlagen zeigt auch das CLI am Ende jedes Laufs an.

## Aufruf (CLI)

```bash
./venv/bin/python audit.py leads.csv --out ergebnis
```

Schreibt `ergebnis.csv` (nach Score absteigend) und `ergebnis.html`
(eine einzelne Datei, im Browser sortier- und filterbar).

### Optionen

| Flag | Vorgabe | Wirkung |
|---|---|---|
| `--out NAME` | `ergebnis` | Basisname der Ausgabe → `NAME.csv` und `NAME.html` |
| `--pagespeed` | aus | Google PageSpeed Insights mitprüfen, braucht `PSI_KEY` |
| `--refresh` | aus | Cache ignorieren, alles neu prüfen |
| `--cache PFAD` | `.site-audit-cache.sqlite` | Ort der Cache-Datenbank |
| `--no-cache` | aus | Gar nicht cachen |
| `--max-age N` | `0` | Cache-Einträge älter als N Tage neu prüfen (`0` = nie) |
| `--concurrency N` | `30` | Wie viele Firmen gleichzeitig geprüft werden |
| `--host-interval S` | `1.0` | Mindestabstand zwischen Requests an denselben Host |
| `--assets N` | `6` | Wie viele Bilder/CSS/JS pro Seite vermessen werden, Inhaltsbilder zuerst (`0` = aus) |
| `--timeout S` | `20` | Timeout pro Request |

### Tempo

Eine Firma braucht rund 9 s, fast alles davon ist Warten auf den Takt von
1 Request pro Sekunde und Host. Der Durchsatz ist deshalb *Parallelität geteilt
durch Dauer pro Firma*. Gemessen an 240 zufälligen Firmen aus einer echten
Liste: **184 Firmen pro Minute** statt 35 mit den früheren Vorgaben (10 parallel,
15 Stichproben, doppelte Abrufe, volle Wartezeit bei toten Adressen).

Was das Tempo bringt, ohne die Höflichkeit zu lockern:

- **`--concurrency 30`.** Jede Firma liegt auf einem anderen Host, mehr
  parallele Firmen belasten also keine einzelne Seite stärker. Der Takt pro
  Host gilt unverändert. In der GUI steht die Zahl unten rechts („Parallel").
- **`--assets 6` statt 15.** Jede Datei kostet eine Sekunde Takt. Die Dateien
  werden jetzt gleichzeitig angestoßen, Dateien auf verschiedenen Hosts
  warten nicht mehr aufeinander. Preis: Das Seitengewicht ist eine
  Untergrenze aus weniger Dateien; an 33 Testseiten blieben 23 Scores
  unverändert, die mittlere Abweichung lag bei 0,8 Punkten (meist −2,5 durch
  ein entfallenes Gewichts-Finding). Für eine genauere zweite Runde über die
  besten Kandidaten: `--assets 15`.
- **Tote Adressen sofort abhaken.** „Name or service not known" wird nicht
  wiederholt, der http-Ausweichversuch entfällt, und schon der robots.txt-
  Abruf merkt sich den Host. Timeouts werden einmal wiederholt, der
  Verbindungsaufbau gibt nach 8 s auf. Vorher hielt eine tote Seite einen
  Platz bis zu 170 s.
- **Impressum mit einem Abruf** statt HEAD plus GET.

Bei sehr hoher Parallelität (> 60) lohnt ein Blick auf die eigene Leitung. Wer
die frühere Vorsicht will: `--concurrency 10`.

## Firmen finden

Das Tool bewertet nicht nur eine fertige Liste, es findet auch Firmen. Quelle ist
OpenStreetMap (Overpass-API, ODbL). Es werden nur Betriebe geliefert, bei denen
eine Website hinterlegt ist, denn ohne Website gibt es nichts zu prüfen.

In der Oberfläche: **Firmen suchen …** → Bundesland, optional Kreis oder
kreisfreie Stadt, Branchen wählen. Die Treffer werden an die offene Liste
**angehängt** (so lassen sich mehrere Kreise zusammentragen) und als
`leads.csv` gesichert. Auf der Kommandozeile:

```bash
./venv/bin/python finden.py --region vulkaneifel --branchen handwerk,gastronomie --out leads.csv
```

Größte sinnvolle Einheit ist ein Bundesland. Rheinland-Pfalz liefert allein im
Handwerk über 8000 Betriebe; ganz Deutschland wäre das Sechzehnfache und lässt
sich weder von Overpass abfragen noch abtelefonieren.

**Kreislisten** liegen als `kreise_de.json` bei: 398 Kreise und kreisfreie
Städte (Berlin und Hamburg haben keine Kreisebene), ohne Netz nutzbar. Der
Dialog hängt deshalb nie am Dienst. Zwei Besonderheiten der Quelle sind
berücksichtigt: Der Landkreis Greiz führt in OpenStreetMap nur den amtlichen
Gemeindeschlüssel (der Gebietsfilter greift deshalb auf beide Schlüssel zu),
und Hanau (06415) ist kein Kreis. Nach einer Gebietsreform erneuern:

```bash
./venv/bin/python finden.py --kreise-erneuern
```

Das fragt Overpass mit Pausen ab; schlägt ein Land fehl, bleibt dessen alter
Eintrag stehen.

**Overpass ist ein gespendeter Dienst** und antwortet unter Last mit 429/504.
Die Abfrage liest vorab den Status, hält die genannte Wartezeit ein und nennt
bei Misserfolg *alle* Server mit ihrer Meldung. Die Suche läuft in der GUI im
Hintergrund (Sekundenanzeige, jederzeit abbrechbar); das Schließen des
Dialogs während einer Abfrage ist gefahrlos.

## CSV-Format

Spalten: `name`, `url`, `ort`, `branche`. Nur `url` ist Pflicht, der Rest
dient zum Filtern im HTML-Report. Groß-/Kleinschreibung der Spaltennamen ist
egal, Komma und Semikolon werden beide erkannt.

```csv
name,url,ort,branche
Info-Terra,info-terra.de,Vulkaneifel,Dienstleistung
Landgasthaus Pfahl,landgasthaus-pfahl.de,Eifel,Gastronomie
```

Die URL darf ohne Schema stehen. Dann wird erst `https://` versucht und bei
einem Fehler `http://`.

## Chaos-Score

0–100, **höher = schlechter**. Jeder Check liefert eine Schwere zwischen 0 und
1, die mit ihrem Gewicht multipliziert wird. Die Gewichte stehen als Dict ganz
oben in [audit.py](audit.py) und sind zum Anpassen gedacht:

| Check | Gewicht | Was geprüft wird |
|---|---|---|
| `erreichbarkeit` | 12 | Timeout, HTTP-Fehler, Redirect-Ketten über 2, lahme Antwort |
| `https` | 14 | nur http, Zertifikat abgelaufen/ungültig, Mixed Content |
| `mobil` | 18 | Viewport-Meta fehlt, Zoom gesperrt, feste Breite |
| `aktualitaet` | 9 | Copyright-Jahr vor 2022, alter `Last-Modified`-Header |
| `technologie` | 16 | Jimdo, Wix, Webnode, IONOS, Weebly, altes WordPress/Joomla/TYPO3, Slider Revolution (Sicherheitsargument nur mit Versionsnachweis), Flash, Frames, Tabellenlayout |
| `recht` | 15 | Impressum-/Datenschutz-Link, Consent-Banner, Google Fonts direkt von Google |
| `seo` | 9 | Title, Meta-Description, H1-Anzahl, Anteil Bilder ohne alt |
| `bilder` | 6 | Seitengewicht, Einzelbilder über 1,5 MB, Anteil Inhaltsbilder unter 400 px (erst ab 3 Inhaltsbildern) |
| `kontakt` | 6 | kein `tel:`-Link, kein Kontaktformular |
| `konsistenz` | 3 | Telefonnummer in der Meta-Description weicht vom Seitentext ab |
| `pagespeed` | 5 | nur mit `--pagespeed`, mobiler Score der PSI-API |

Checks, die nicht laufen konnten, fallen aus der Rechnung heraus — `--pagespeed`
verschiebt die Skala also nicht.

`pagespeed` ist absichtlich das zweitniedrigste Gewicht. PSI misst
Geschwindigkeit, nicht Zeitgemäßheit, und beides fällt bei dieser Zielgruppe
auseinander: eine Jimdo-Seite von 2011 ist winzig und lädt schnell, eine
moderne Seite mit viel Bildmaterial ist schwer. Über die fünf Testseiten
korreliert der PSI-Wert mit **+0,82** zum Chaos-Score — ein hohes Gewicht würde
also genau die besten Leads nach unten ziehen. Mit 5 liefert der Check noch
brauchbare Gesprächsargumente („Google bewertet Ihre Seite mit 27 von 100"),
ohne die Rangfolge zu verdrehen.

Bezugsgröße ist nicht die volle Gewichtssumme, sondern `SCORE_REFERENZ` (75),
ebenfalls oben in der Datei. Grund: keine reale Seite fällt in allen elf
Kategorien gleichzeitig durch. Mit der vollen Summe als Nenner landet selbst
eine ruinierte Seite nur bei etwa 45, und die Skala wird stumpf. 75 entspricht
„in den vier bis fünf schwersten Kategorien komplett durchgefallen". Kleiner
spreizt stärker nach oben, größer macht milder.

Zur Orientierung: ab **35** gelb, ab **60** rot.

### Nicht erreichbare Seiten

Seiten, die sich nicht öffnen lassen (Timeout, DNS-Fehler, Verbindungsfehler,
HTTP 5xx, durch robots.txt gesperrt), bekommen **keinen Chaos-Score**, sondern
den Status `nicht erreichbar` samt Grund. Sie sortieren immer ans Ende der
Liste (in jeder Sortierung), und ab „Score ab X" mit X > 0 tauchen sie nicht
mehr auf; bei „Score ab 0" bleiben sie sichtbar, damit sie auffindbar sind.

Der Grund: Eine tote Adresse sagt nichts über die Qualität der Seite, und in
den Quelldaten stecken viele Tippfehler und veraltete Einträge. Mit Score 100
standen sie sonst ganz oben, obwohl kein Anruf dahinter steht. In einem
Testlauf waren **122 von 1518** geprüften Adressen betroffen, davon 69 mit
DNS-Fehler. Die Empfehlung lautet deshalb immer „manuell ansehen" — erst im
Browser prüfen, ob die Adresse stimmt. Verkaufsargumente gibt es für sie nicht.

Ältere Cache-Einträge (`fehler`, `gesperrt`, `ok` mit HTTP 5xx) werden beim
Lesen automatisch auf das neue Schema umgestellt, ohne neu zu prüfen.
Bleibt eine Seite bei HTTP 4xx erreichbar, wird sie weiter bewertet.

### Slider Revolution

Das Plugin als Argument zu nennen setzt einen **Versionsnachweis** voraus. Er
kommt aus dem Generator-Meta, dem `?ver=`-Parameter an Plugin-Dateien, einem
Kommentar im Quelltext oder (nur die Hauptnummer) aus dem Dateipfad
(`sr6/`, `rs6.min.js`; der Ordner `rs-plugin` gehört zu den Reihen 4 und 5).
Nur wenn die Hauptversion unter `SLIDERREV_AKTUELLE_HAUPT` in
[audit.py](audit.py) liegt, entsteht ein Sicherheitsargument, und auch das nur
als Hinweis („sollte geprüft werden"). Reiner Fingerabdruck ohne Version
bleibt als Befund `[Technik]` stehen, ohne Argument. Stand Dezember 2025 ist
die Reihe **6.x** die aktuelle (Plugin 6.7.40); sie wird gepflegt und gilt
nicht als veraltet. Die Konstante muss angepasst werden, falls Themepunch die
Plugin-Hauptnummer auf 7 hebt.

### Bildprüfung

Gezählt werden nur **Inhaltsbilder**. Lazy-Load-Platzhalter (`data:`-URIs,
`a3-lazy-load`, `lazy_placeholder.gif`, `dummy.png`, 1×1-GIFs) werden
übersprungen; die Breite kommt aus dem größten Element von `srcset` bzw.
`data-srcset`, sonst aus `data-src`, `data-lazy-src`, `data-original`,
`data-lazyload` und zuletzt `src`. Logos, Icons, Zählpixel, SVGs sowie Bilder
in Kopf, Navigation, Fuß und Seitenleiste zählen nicht. „Bilder zu klein" gibt
es erst ab mindestens drei bewerteten Inhaltsbildern und wenn mehr als die
Hälfte schmaler als 400 px ist.

### Spalte `argumente`

Zwei bis vier fertige Sätze fürs Erstgespräch, automatisch aus den Befunden
gebaut und nach Gewicht × Schwere sortiert. Rechtliche Punkte sind bewusst als
Hinweis formuliert („sollte geprüft werden") und nie als Rechtsbehauptung.
Das Tool ist ein Rechercheassistent, keine Rechtsauskunft.

## PageSpeed Insights

```bash
cp .env.example .env     # PSI_KEY eintragen
./venv/bin/python audit.py leads.csv --pagespeed
```

Der Key wird aus der Umgebungsvariable `PSI_KEY` gelesen; eine `.env` im
Arbeitsverzeichnis wird zusätzlich eingelesen. Ohne Key wird der Check
übersprungen, der Lauf bricht nicht ab. PSI ist deutlich langsamer als alles
andere — für große Listen besser nur auf die Top-Kandidaten anwenden.

## Verhalten im Netz

- Ehrlicher User-Agent: `site-audit/1.0 (+paeffgen-it.de)`
- `robots.txt` wird respektiert
- Standardmäßig 30 Firmen gleichzeitig, aber weiterhin maximal 1 Request pro Sekunde und Host
- Pro Seite nur die Startseite plus ein Blick auf den Impressum- und
  Datenschutz-Link. Kein Crawlen, keine Logins, keine abgeschickten Formulare
- Netzfehler werden bis zu dreimal wiederholt, bevor eine Seite als nicht
  erreichbar gilt. Ohne das kippt ein einzelner Aussetzer einen Lead auf
  Score 100 — oder lässt eine https-Seite fälschlich als „nur http" erscheinen
- Fehler werden pro Seite aufgefangen; ein Lauf bricht nie komplett ab

## Cache

Ergebnisse landen in einer SQLite-Datei, ein zweiter Lauf prüft deshalb nur
neue URLs (fünf Seiten: 27 s → 0,3 s). `--refresh` erzwingt eine Neuprüfung,
`--max-age N` lässt Einträge nach N Tagen verfallen. Stammdaten wie Ort und
Branche kommen immer frisch aus der CSV, nur die Bewertung stammt aus dem Cache.

## Tests

```bash
./venv/bin/python -m pytest
```

457 Tests gegen gespeicherte HTML-Fixtures in [tests/fixtures/](tests/fixtures/) —
kein Netzzugriff. Abgedeckt sind die Score-Logik (Kombination, Normierung,
Argumentbau), alle Checks, die Parser für Bildmaße, Telefonnummern und
Copyright-Jahre, Rate-Limit, Cache, Statusbuch und Ausgabe sowie die GUI
(Filter, Statuspflege, PDF-Export) offscreen über `QT_QPA_PLATFORM=offscreen`.

## Dateien

| Datei | Zweck |
|---|---|
| [audit.py](audit.py) | Prüflogik und CLI, Gewichte ganz oben |
| [finden.py](finden.py) | Firmensuche (OpenStreetMap) und Kreislisten |
| [kreise_de.json](kreise_de.json) | mitgelieferte Kreisliste aller Bundesländer |
| [gui.py](gui.py) | Qt-Oberfläche (PySide6) |
| [install-desktop.sh](install-desktop.sh) | Menü- und Taskleisteneintrag anlegen |
| [desktop/](desktop/) | Vorlage für den `.desktop`-Eintrag |
| [assets/](assets/) | Anwendungssymbol |
| [templates/report.html.j2](templates/report.html.j2) | HTML-Report |
| [SPEC.md](SPEC.md) | ursprüngliche Vorgabe |
| [tests/](tests/) | Tests und HTML-Fixtures |
