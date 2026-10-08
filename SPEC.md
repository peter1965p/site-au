# site-audit — Spezifikation

Stand: 2026-10-08. Quelle: Vorgabe von Peter (Päffgen IT).

## Zweck

Eine Liste von Firmen-Webseiten automatisch auf Relaunch-Bedarf prüfen und die
schlechtesten nach oben sortieren. Einsatz: Akquise für Päffgen IT, Region
Vulkaneifel/Eifel/Bremen/Bremerhaven/Wurster Nordseeküste.

## Eingabe

- CSV mit Spalten: `name, url, ort, branche`
  (url ohne Schema erlaubt → `https://` annehmen, bei Fehler `http://` probieren)
- Aufruf: `python audit.py leads.csv --out ergebnis`

## Checks

Jeder Check liefert Punkte + einen kurzen Klartext-Befund auf Deutsch.

1. **Erreichbarkeit** — Timeout, HTTP-Fehler, Redirect-Ketten > 2
2. **HTTPS** — nur http, Zertifikat abgelaufen oder ungültig, Mixed Content
3. **Viewport** — Meta fehlt oder `user-scalable=0` gesetzt
4. **Aktualität** — Copyright-Jahr im Footer älter als 2022 (Regex),
   zusätzlich `Last-Modified`-Header falls vorhanden
5. **Baukasten/CMS-Fingerabdruck** — Jimdo, Wix, Webnode, IONOS MyWebsite,
   Weebly, WordPress (Version aus Generator-Meta, alte Versionen markieren),
   Joomla, TYPO3, "Slider Revolution", Flash/Frames/Tabellenlayout
   (viele verschachtelte `<table>`, `<frameset>`, `<font>`)
6. **Rechtliches** — Impressum- und Datenschutz-Link fehlt; Cookie-Banner fehlt,
   obwohl Google Fonts/Analytics/Maps eingebunden sind; Google Fonts direkt von
   `fonts.googleapis.com` geladen (DSGVO-Risiko, starkes Verkaufsargument)
7. **SEO-Basics** — Title fehlt/zu lang/Keyword-Stuffing (Wiederholungen),
   Meta-Description fehlt, mehr als ein H1 oder keins, Bilder ohne alt (Anteil)
8. **Bilder** — Anteil Bilder unter 400 px Breite im sichtbaren Content,
   Gesamtgewicht der Seite, unkomprimierte Riesenbilder (> 1,5 MB einzeln)
9. **Kontakt** — kein Kontaktformular und keine klickbare Telefonnummer (`tel:`)
10. **Textauffälligkeiten** — abweichende Telefonnummern zwischen
    Meta-Description und Seitentext
11. **PageSpeed (optional, `--pagespeed`)** — Google PageSpeed Insights API,
    Key aus Umgebungsvariable `PSI_KEY`, mobile Score. Ohne Key überspringen,
    **nicht** abbrechen. `.env.example` mit `PSI_KEY=` anlegen.

## Score

- Gewichteter "Chaos-Score" 0–100 (höher = schlechter)
- Gewichte als Dict **oben in der Datei**, damit sie anpassbar sind
- Zusätzlich Spalte `argumente`: 2–4 konkrete, kurze Sätze für das
  Erstgespräch, automatisch aus den Befunden gebaut
  (z. B. "Seite nicht mobiltauglich, kein Viewport-Tag",
  "Google Fonts ohne Einwilligung eingebunden")
- **Rechtliche Punkte nur als Hinweis formulieren** ("sollte geprüft werden"),
  nie als Rechtsbehauptung.

## Ausgabe

- `ergebnis.csv` — sortiert nach Score absteigend
- `ergebnis.html` — eine einzelne Datei, Tabelle sortier- und filterbar nach
  Ort/Branche, Score farbig, Befunde ausklappbar, dunkles Theme
- Fortschrittsanzeige im Terminal (rich)

## Technik und Verhalten

- httpx + selectolax, asyncio mit Semaphore (max 10 parallel,
  max 1 Request/Sekunde pro Host)
- robots.txt respektieren (`urllib.robotparser`)
- Ehrlicher User-Agent: `site-audit/1.0 (+paeffgen-it.de)`
- Pro Seite nur Startseite plus Impressum/Datenschutz-Link-Check.
  Kein Crawlen der ganzen Seite, keine Logins, keine Formulare absenden
- Fehler pro Seite abfangen, nie den ganzen Lauf abbrechen; nicht erreichbare
  oder durch robots.txt gesperrte Seiten landen mit passendem Befund in der Liste
- Ergebnisse cachen (SQLite), damit ein zweiter Lauf nur neue URLs prüft;
  Flag `--refresh` zum Erzwingen

## Abnahme (selbst ausführen, nicht nur beschreiben)

1. Test-CSV mit diesen fünf Seiten anlegen:
   `info-terra.de`, `landgasthaus-pfahl.de`, `haus-anny.de`,
   `haus-sonneck-eifel.de`, `frese-salbert.de`
2. Tool darauf laufen lassen, Konsolenausgabe und sortierte Tabelle zeigen
3. Plausibilität prüfen: **info-terra.de (Jimdo) soll deutlich höher scoren als
   frese-salbert.de.** Wenn nicht: Checks und Gewichte nachjustieren, bis es stimmt
4. pytest-Tests für Score-Logik und Parser mit gespeichertem HTML (Fixtures),
   laufen lassen
5. Kurze README mit Aufruf, CSV-Format und Gewichten
