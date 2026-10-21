# Patroontijden

Departure-pattern tables ("Vertrek / Serie / Bestemming / Spoor / Soort / Vervoerder / Opmerkingen")
per Dutch railway station, generated from the OVapi GTFS feed.

```bash
python3 build.py --download                 # fetch gtfs-nl.zip; new timetable if the feed has it, else current
python3 build.py --start 2026-10-25         # any other Sunday; all whole weeks the feed covers (max 8)
python3 build.py --start 2026-11-15 --weeks 1
python3 -m http.server 8765          # then open http://localhost:8765/#asd
```

`build.py` takes a reference period of whole weeks (Sunday–Saturday). Per weekday it keeps the departures
that run on the majority of those dates (so engineering works and one-off extras drop out), then folds them
into rows per serie + minute past the hour. Remarks (`ma-do tot 22:00; za na 10:00`, rounded to whole hours) describe on which
days and between which times a row typically runs. Directions are named after the first
Intercity station (≥ 7 km away) the train calls at or passes through.

## Deployment

GitHub Actions ([.github/workflows/pages.yml](.github/workflows/pages.yml)) downloads the latest feed, runs
`build.py` and publishes `index.html` + `data/` to GitHub Pages every other day, on every push to `main`, and on
demand via *Run workflow*. Without `--start` the build uses the new timetable (second Sunday of December) as soon
as the feed covers it, and the current timetable until then.

Renamed, moved and merged directions are stored per browser (localStorage), not in the repo.
