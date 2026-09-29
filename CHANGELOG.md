# Changelog

Keep a Changelog format, and SemVer as closely as a CLI toolkit can manage:
a patch release means fixes, not a promise that no default ever moves — a
behaviour change in one is announced at the top of its notes.

## [0.1.0] — 2026-09-29

First release.

### What it does

- Full records from Rosreestr's online reference service
  (`lk.rosreestr.ru/eservices/real-estate-objects-online`) by cadastral
  number: cadastral value, object kind, area and main characteristics,
  status, registration and valuation dates, floor, purpose, permitted use,
  land category, old numbers, rights and encumbrances.
- The service's free address search (`--mode address`): every object at an
  address as a list line, no captcha; `--details` adds full records.
- The site's own code dictionaries (`--mode dictionaries`), shipped as a
  snapshot in `rosreestr_codes.json`.
- A captcha scan across the site's pages (`--mode scan`), with the Scraping
  Browser's auto-solve given a turn on each.
- Engines: Playwright (primary), pyppeteer, Selenium, and the 2Captcha
  Scraper API for the free half. One lookup flow, one parser, one CLI.
- Captchas: `Captcha.setAutoSolve` first, 2Captcha `ImageToTextTask` second,
  `reportIncorrect` on a refused answer, a hard `--max-solves` cap.
- `diff_runs.py` for monitoring a fixed list between runs.

### Measured before release (2026-09-29)

Thirteen engine runs through a Russian exit ended with exit 0: full records
through Playwright and pyppeteer over both the Scraping Browser and a
residential proxy, the address search through three engines (plus one run
with `--details`), the dictionaries through two, and two captcha scans. 9
paid image solves, 1 refused and re-solved. Selenium was not run against the
site (it can use neither the Scraping Browser nor an authenticated proxy);
it passes the end-to-end suite against `tests/mock_site.py`.

### Deliberately not done

- The page's other three search types (restriction-of-right number,
  previously assigned number, right number): their request bodies were not
  observed, and this repo does not guess one.
- An object-kind filter on the site's side: the address search ignores every
  `objType` value it is sent (measured), so `--list-kind` filters locally.
- The cadastral engineer's name, phone and certificate number are never
  read into a row.
