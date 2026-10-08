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

## Aufruf

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
| `--concurrency N` | `10` | Wie viele Seiten gleichzeitig |
| `--host-interval S` | `1.0` | Mindestabstand zwischen Requests an denselben Host |
| `--assets N` | `15` | Wie viele Bilder/CSS/JS pro Seite vermessen werden (`0` = aus) |
| `--timeout S` | `20` | Timeout pro Request |

`--assets` bestimmt die Laufzeit stärker als alles andere, weil jedes Asset
dem Rate-Limit von einem Request pro Sekunde und Host unterliegt. Für große
Listen lohnt `--assets 5`; dann entfällt die Aussage zum Seitengewicht
weitgehend, der Rest bleibt vollständig.

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
| `technologie` | 16 | Jimdo, Wix, Webnode, IONOS, Weebly, altes WordPress/Joomla/TYPO3, Slider Revolution, Flash, Frames, Tabellenlayout |
| `recht` | 15 | Impressum-/Datenschutz-Link, Consent-Banner, Google Fonts direkt von Google |
| `seo` | 9 | Title, Meta-Description, H1-Anzahl, Anteil Bilder ohne alt |
| `bilder` | 6 | Seitengewicht, Einzelbilder über 1,5 MB, Anteil Bilder unter 400 px |
| `kontakt` | 6 | kein `tel:`-Link, kein Kontaktformular |
| `konsistenz` | 3 | Telefonnummer in der Meta-Description weicht vom Seitentext ab |
| `pagespeed` | 10 | nur mit `--pagespeed`, mobiler Score der PSI-API |

Checks, die nicht laufen konnten, fallen aus der Rechnung heraus — `--pagespeed`
verschiebt die Skala also nicht.

Bezugsgröße ist nicht die volle Gewichtssumme, sondern `SCORE_REFERENZ` (75),
ebenfalls oben in der Datei. Grund: keine reale Seite fällt in allen elf
Kategorien gleichzeitig durch. Mit der vollen Summe als Nenner landet selbst
eine ruinierte Seite nur bei etwa 45, und die Skala wird stumpf. 75 entspricht
„in den vier bis fünf schwersten Kategorien komplett durchgefallen". Kleiner
spreizt stärker nach oben, größer macht milder.

Zur Orientierung: ab **35** gelb, ab **60** rot. Eine nicht erreichbare Seite
bekommt 100 — sie ist der stärkste Anlass für einen Anruf. Eine durch
robots.txt gesperrte Seite bekommt 0 und den Hinweis, sie manuell anzusehen;
sie wäre sonst nur eine Lücke in der Bewertung, die wie ein gutes Ergebnis
aussieht.

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
- Maximal 10 Seiten gleichzeitig, maximal 1 Request pro Sekunde und Host
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

141 Tests gegen gespeicherte HTML-Fixtures in [tests/fixtures/](tests/fixtures/) —
kein Netzzugriff. Abgedeckt sind die Score-Logik (Kombination, Normierung,
Argumentbau), alle Checks, die Parser für Bildmaße, Telefonnummern und
Copyright-Jahre sowie Rate-Limit, Cache und Ausgabe.

## Dateien

| Datei | Zweck |
|---|---|
| [audit.py](audit.py) | das gesamte Tool, Gewichte ganz oben |
| [templates/report.html.j2](templates/report.html.j2) | HTML-Report |
| [SPEC.md](SPEC.md) | ursprüngliche Vorgabe |
| [tests/](tests/) | Tests und HTML-Fixtures |
