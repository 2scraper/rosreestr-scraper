# Changelog

Keep a Changelog format, and SemVer as closely as a CLI toolkit can manage:
a patch release means fixes, not a promise that no default ever moves — a
behaviour change in one is announced at the top of its notes.

## [0.1.1] — 2026-09-29

Fixes from a third-party audit of 0.1.0 (2026-09-29), each reproduced on
`main` before it was changed, each pinned by a check that goes red when the
old defect is planted back.

> **Behaviour changes for an existing user:**
>
> - A malformed answer — an error object from the address search, a non-2xx
>   status, records without a cadastral number — is now a **failure** (exit
>   5, or 6 in a run that got other answers). 0.1.0 read it as "nothing
>   found", and with `--allow-empty` wrote an empty table marked complete.
> - `diff_runs.py` now refuses a run without the new `query_spec`, which
>   includes every 0.1.0 output. Re-run both sides, or pass `--force`.
> - `--proxy-rotate per-page` outside `--mode cadastral` is a usage error;
>   it was silently ignored.

### Fixed

- **A malformed answer read as "not found".** The address search's answer
  must be a JSON array and `/on`'s status must be 2xx; elements without a
  cadastral number are counted in `malformed_rows` instead of vanishing, and
  an answer holding nothing readable fails the query.
- **`diff_runs.py` compared two different questions.** The sidecar records a
  `query_spec` (service URL, mode, `--list-kind`, `--details`,
  `--max-objects`), and a diff requires it to match — a PARCEL-only and a
  FLAT-only run of one address are not a before and an after.
- **The wheel could not decode anything.** The code dictionaries were a JSON
  file beside the scripts, left out of the wheel (`FileNotFoundError` in any
  installed copy). They are now the generated module `rosreestr_codes.py`,
  and CI builds the wheel, installs it in a clean venv and decodes outside
  the checkout.
- **A list line could replace a paid full record.** When two addresses
  overlap, the full record now wins, in the place the object first
  appeared, and an object already fetched in full is not paid for again.
- **What a query cost could vanish.** Solves counted before a driver error
  are kept; a task 2Captcha refused to create is no longer counted as a
  solve; the sidecar carries 2Captcha's own reported `captcha_cost`.
- **`--mode scan` claimed "solved" on faith.** It now re-reads the page and
  reports `solved` only when the challenge is gone (`token_injected` and
  `site_verified` are separate fields).
- **`--delay` did not apply between addresses**, nor between numbers under
  `--proxy-rotate per-page`.
- **`--dump-html` wrote the cadastral engineer's name and phone** into the
  traffic dump (`queryN.net.json`); they are now scrubbed there as in the
  fixtures.

### Added

- `status_code`: the site's raw status code beside the mapped `status`, so
  a code nobody has seen is kept rather than folded into "cancelled".

### Not changed, and why

- **A scheduled canary.** GitHub runners are outside Russia and Scraping
  Browser credentials expire in about a day, so a schedule would go red on
  a stale secret rather than on the site; it stays dispatch-only.
- **A landing page in this repository.** The landing page is for
  2captcha.com and lives beside the repository, as in the rest of the family.
- **An HTTP transport, a `Protocol` for the session, checkpoints.** Worth
  doing, not fixes. A plain HTTP client through the Russian proxy did not
  get through when tried during 0.1.0's development, so an HTTP transport
  needs its own measurement before anything is claimed.

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
