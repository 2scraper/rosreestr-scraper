# rosreestr-scraper

[![release](https://img.shields.io/github/v/release/2scraper/rosreestr-scraper)](https://github.com/2scraper/rosreestr-scraper/releases)
[![tests](https://github.com/2scraper/rosreestr-scraper/actions/workflows/tests.yml/badge.svg)](https://github.com/2scraper/rosreestr-scraper/actions/workflows/tests.yml)
[![canary](https://github.com/2scraper/rosreestr-scraper/actions/workflows/canary.yml/badge.svg)](https://github.com/2scraper/rosreestr-scraper/actions/workflows/canary.yml)
![python](https://img.shields.io/badge/python-3.9%E2%80%933.12-blue)
[![licence](https://img.shields.io/badge/licence-MIT-green)](LICENSE)
![engines](https://img.shields.io/badge/engines-Playwright%20%C2%B7%20Selenium%20%C2%B7%20Puppeteer%20%C2%B7%20Scraper%20API-informational)
![exit](https://img.shields.io/badge/needs-a%20Russian%20exit-orange)

Look real-estate objects up in **Rosreestr's online reference service**
([lk.rosreestr.ru — «Справочная информация по объектам недвижимости в режиме
online»](https://lk.rosreestr.ru/eservices/real-estate-objects-online)) and
get JSON/CSV back: cadastral value, object type, area, status, registration
and valuation dates, floor, purpose, permitted use, and the registered rights
and encumbrances — by **cadastral number**, or **every object at an address**.

Playwright is the primary engine; Selenium, pyppeteer and the 2Captcha
Scraper API run the same flow and write the same rows.

```bash
python3 playwright_scraper.py --cad-number 77:01:0001044:3030
```

```json
{
  "sku": "77:01:0001044:3030",
  "title": "Москва, Тверской, ул. Тверская, д. 13, пом. III",
  "price": 123431740.67, "currency": "RUB",
  "category": "Помещение", "area": 479.8, "area_unit": "кв.м",
  "status": "actual", "level_floor": "5", "purpose": "Нежилое",
  "reg_date": "2013-03-22", "cad_cost_determination_date": "2025-01-01"
}
```

## Two things to know first

**1. The service answers only Russian addresses.** From a Belgian exit on
2026-09-29 every Rosreestr host timed out at the TCP connect — no refusal
page, no status, silence. You need a Russian exit: a
[Scraping Browser API](https://2captcha.com/scraper/browser-api) profile with
`country-ru`, or a [2Captcha proxy](https://2captcha.com/proxy) (also sold as
2prx.com) with a `-region-ru` login.

**2. Every full record costs one image captcha.** The page says so itself —
one captcha "позволяет просмотреть информацию об одном объекте" — and it is
one-use: re-sending a spent answer returns `406 Wrong captcha`. The
**address search is free**: it lists every object at an address (number,
address, kind, current or not) with no captcha at all.

No Gosuslugi (ESIA) login is needed or used.

## What was measured (2026-09-29)

Thirteen engine runs through a Russian exit ended with exit 0. Figures come
from each run's own `.meta.json`; dictionary and scan runs write their own
JSON instead and have none.

| Run | Engine | Route | Result |
|---|---|---|---|
| 3 cadastral numbers | Playwright | Scraping Browser | 2 records + 1 "no such object", complete; 4 solves, 1 refused by the site and re-solved |
| 1 cadastral number | Playwright | residential proxy (`--local`) | 1 record, 1 solve |
| 1 cadastral number | pyppeteer | Scraping Browser | 1 record, 1 solve |
| 1 cadastral number | pyppeteer | residential proxy (`--local`) | 1 record, 1 solve |
| address, list (×4) | Playwright ×2, pyppeteer, Scraper API | Scraping Browser | 100 lines each, 0 captchas |
| address + `--details` | Playwright | Scraping Browser | 100 lines, 2 of them full records, 2 solves |
| dictionaries (×2) | Playwright, Scraper API | Scraping Browser | 5 dictionaries, 0 captchas |
| `--mode scan` (×2) | Playwright | Scraping Browser | the service page's image captcha found (by the second run; the first read the page before it had painted, since fixed) |

Across them: **9 paid image solves, 1 refused**, each **$0.001**, taking
**7.1 to 61.4 s** where the log kept the timing. The runs that failed —
Selenium through a login proxy, the Scraper API's own exits, the Scraping
Browser answering HTTP 500 for a few minutes — are described in
[TROUBLESHOOTING.md](TROUBLESHOOTING.md). Selenium, which can use neither
the Scraping Browser nor a login proxy, was not run against the site; it
passes the same end-to-end suite against a local replica.

## Install

One engine per virtualenv — the engines' own dependency pins collide.

```bash
python3 -m venv venv && . venv/bin/activate
pip install --require-hashes -r requirements-playwright.lock   # or: pip install -r requirements.txt -r requirements-playwright.txt
python -m playwright install chromium
cp .env.example .env    # then fill in what you have
python3 env_config.py   # says what was picked up, never the values
```

`.env`:

| Variable | What |
|---|---|
| `TWOCAPTCHA_KEY` | 2Captcha API key — pays for the image captchas (and the Scraper API) |
| `ROSREESTR_CDP_ENDPOINT` | `ws://{login}-zone-scraping_browser-country-ru-pid-{profileId}:{password}@cb.2captcha.com:9222` |
| `ROSREESTR_PROXY` | `http://{user}:{password}@ru.proxy.2captcha.com:2334` with a `-region-ru` login |
| `ROSREESTR_URL` | the service page; leave the default |

With both an endpoint and a proxy in `.env`, the Scraping Browser is used;
`--local` launches your own browser on the proxy instead. Credentials never
go on the command line.

## Usage

```bash
# full records, one captcha each
python3 playwright_scraper.py --cad-number 77:01:0001044:3030 --cad-number 77:01:0001044:2981
python3 playwright_scraper.py --input numbers.txt --out objects      # one per line, # comments ok

# every object at an address — free
python3 playwright_scraper.py --mode address --address "Москва, ул. Тверская, д. 13"
python3 playwright_scraper.py --mode address --address "..." --list-kind PARCEL
python3 playwright_scraper.py --mode address --address "..." --details --max-objects 20

# the site's code dictionaries: the nine object kinds, land categories, permitted uses, purposes
python3 playwright_scraper.py --mode dictionaries --out codes

# look for a captcha on the site's pages and let the Scraping Browser's auto-solve try each
python3 playwright_scraper.py --mode scan

# the free half with no browser at all
python3 scraper_api_client.py --mode address --address "Москва, ул. Тверская, д. 13"
```

Output: `<out>.json`, `<out>.csv` and `<out>.meta.json` (default `--out
rosreestr_objects`).

### The nine object kinds

The brief's "all categories" are the site's own `OBJECT_TYPE_CODES`, all of
which a lookup returns: Земельный участок, Здание, Помещение, Сооружение,
Объект незавершенного строительства, Предприятие как имущественный комплекс,
Единый недвижимый комплекс, Машино-место, Иной объект недвижимости.

### Options

| Flag | Default | |
|---|---|---|
| `--mode` | `cadastral` | `cadastral` · `address` · `dictionaries` · `scan` |
| `--cad-number` / `--input` | | the numbers to look up (repeatable / a file) |
| `--address` | | for `--mode address` (repeatable) |
| `--list-kind` | all | `OKS` / `FLAT` / `PARCEL` — the site's own label; filtered here, see below |
| `--details`, `--max-objects` | off, 20 | full records for the objects an address lists |
| `--captcha-attempts` | 3 | paid solves per record before giving up |
| `--max-solves` | queries × attempts | a hard cap on paid solves for the whole run |
| `--autosolve-wait` | 15 | seconds the Scraping Browser's auto-solve gets first |
| `--solve-captcha never` | | never pay; records then need auto-solve |
| `--report-correct` | off | also report accepted answers (wrong ones always are) |
| `--delay`, `--retries`, `--retry-delay` | 2, 1, 5 | pacing |
| `--cdp-endpoint`, `--proxy`, `--proxy-file`, `--proxy-rotate`, `--local` | | where the browser runs; `--proxy-rotate per-page` is for `--mode cadastral` only |
| `--fingerprint`, `--fp-country`, `--fp-tags`, `--locale` | | identity of a local browser |
| `--headless` / `--headful`, `--dump-html DIR`, `--allow-empty`, `--format` | | |

Every engine takes exactly the same flags — they are built by one parser.

## How a captcha is answered

1. **The Scraping Browser's auto-solve goes first.** Every session over
   `--cdp-endpoint` sends `Captcha.setAutoSolve`; each captcha waits
   `--autosolve-wait` for it. Measured honestly: it has not answered this
   site's image captcha once (0 `Captcha.*` events in every run on
   2026-09-29), so after two silent waits it gets 3 s instead.
2. **Then 2Captcha's image solver** — `ImageToTextTask` with
   `languagePool: "rn"`, because the five characters are Cyrillic more often
   than Latin.
3. **The site checks the answer itself** (`GET /account-back/captcha/{text}`,
   200 or 403). A refused answer is reported (`reportIncorrect`), a fresh
   image is solved — up to `--captcha-attempts`, never past `--max-solves`.

The sidecar records `captcha_solves` (image tasks 2Captcha accepted — a task
it refused, e.g. for a zero balance, is not counted), `captcha_cost` (summed
from 2Captcha's own reported `cost`), `captcha_rejected` and `autosolved`:
the bill, measured. It also records the run's `query_spec` (mode, filters,
`--details` reach), which `diff_runs.py` requires to match, and
`malformed_rows` — elements of the site's answers that were not records and
were left out rather than silently dropped.

`--mode scan` reports a captcha as `solved` only when the page, read again
after the token went in (or after auto-solve said it finished), no longer
shows it; `token_injected` and `site_verified` are recorded separately.

## Output

One row per object. The first ten columns are this scraper family's common
prefix; the rest are Rosreestr's.

| Column | |
|---|---|
| `source`, `scraped_at`, `url` | `url` opens the object through the page's own `?cadNumber=` |
| `sku` | the cadastral number |
| `title` | the address as the site renders it |
| `price`, `currency` | the cadastral value, and `RUB` because the site labels it "(руб)"; both null when the site states no value |
| `category` | the object kind in the site's words (null on list lines it does not state) |
| `page`, `position` | which query of the run, and the position in its answer |
| `query`, `detail_level` | what was asked; `full` (a record) or `list` (an address-search line) |
| `object_type_code`, `status` | `actual` / `cancelled` (list lines: `actual` / `not_actual`) |
| `cad_quarter`, `area`, `area_unit`, `main_characteristics`, `region` | |
| `reg_date`, `cancel_date`, `cad_cost_determination_date`, `cad_cost_registration_date`, `info_update_date` | ISO dates |
| `land_category`, `permitted_use`, `purpose` | decoded through the site's own dictionaries |
| `floors`, `underground_floors`, `level_floor`, `wall_material`, `year_built`, `year_commissioned`, `ownership_type` | |
| `parent_cad_number`, `child_cad_numbers`, `old_numbers` | |
| `rights`, `encumbrances` | `[{number, date, type}]` — never who holds them: the public service does not show it |
| `list_kind` | list lines: the address search's own `OKS` / `FLAT` / `PARCEL` |
| `status_code` | full records: the site's own status code, verbatim (`status` maps `"1"` to actual and anything else to cancelled, as the site's card does) |

**Not in the output, on purpose:** the cadastral engineer's name, phone and
certificate number. The service returns them with every record; they
identify a private person, and no use of this data needs them.

A sample cut from real runs: [`sample_output.json`](sample_output.json) /
[`.csv`](sample_output.csv).

### Exit codes

| | |
|---|---|
| `0` | every query answered (some may be "no such object" — listed in `queries_not_found`) |
| `2` | bad usage |
| `3` | blocked: the captcha kept being refused, or the site refused the request |
| `4` | every query answered "no such object" (or nothing of the `--list-kind` asked); nothing written (`--allow-empty` to write an empty table) |
| `5` | nothing obtained — no Russian exit, a dead proxy, the browser never started, or an answer that was not the site's contract (an error object, a non-2xx status, records without a cadastral number) |
| `6` | partial: some answered, some failed (`queries_failed`, by input position and reason) |

A run that obtains nothing never overwrites a previous good output.

## Traps that look like bugs

- **The address search returns at most 100 lines — and not always the same
  100.** Two calls 25 s apart returned 100 each with only 94 in common, in a
  different order. Such queries are listed in `address_capped`. Narrow the
  address (house, building, room); nothing else helps.
- **The address search ignores the object kind.** `objType=PARCEL`, `OKS`,
  `FLAT` and the object-type codes all returned the same 100 mixed lines, so
  `--list-kind` filters on this side, after the site chose its 100.
- **Some answers are refused.** Of 16 image solves on 2026-09-29 (7 in
  hand-run probes, 9 in engine runs) the site refused 3; why is not always
  clear — one refused answer matched the image as a person reads it. That is
  what `--captcha-attempts` is for; exit 3 means every attempt failed.
- **The Scraping Browser may answer HTTP 500 for a few minutes** right after
  a run on the same profile (one live connection per `pid`); the engines
  try 3 times, 3 s apart. Profile credentials also expire — take fresh ones.
- **`rosreestr.gov.ru` closed the connection** (`ERR_CONNECTION_CLOSED`) on
  every page `--mode scan` tried through the Scraping Browser's Russian exit,
  while `lk.rosreestr.ru` answered. `/login` redirects to Gosuslugi.

More in [TROUBLESHOOTING.md](TROUBLESHOOTING.md).

## Engines

| Engine | Scraping Browser | Authenticated proxy | Live-tested against the site |
|---|---|---|---|
| `playwright_scraper.py` (primary) | yes, with auto-solve | yes | yes, both routes |
| `puppeteer_scraper.py` | yes, with auto-solve | yes (via the CDP Fetch domain) | yes, both routes |
| `selenium_scraper.py` | **no** | **no** | end-to-end suite only |
| `scraper_api_client.py` | through `cdpurl` | — | yes: address + dictionaries |

- **Selenium cannot use the Scraping Browser** — its endpoint authenticates
  on the WebSocket upgrade and chromedriver has nowhere to put a password —
  **nor an authenticated proxy**: Chrome's `--proxy-server` takes no
  credentials (measured: they are dropped with a warning and the exit
  refuses the connection). It needs an IP-allowlisted Russian exit.
- **pyppeteer is effectively unmaintained**; its bundled Chromium is from
  2018, so set `PYPPETEER_EXECUTABLE_PATH` to a current one. Its own
  `page.authenticate()` no longer works on current Chrome (the CDP method it
  uses is gone); this engine answers the proxy's challenge itself.
- **The Scraper API cannot fetch full records.** A record's captcha belongs
  to the page session that showed it; a Scraper API task has no session. Its
  own exits reached the site for a tiny request and timed out on the address
  search (twice), so it is routed through your Scraping Browser via `cdpurl`.

## What the paid products buy you here

| Product | What it does on this site |
|---|---|
| [Scraping Browser API](https://2captcha.com/scraper/browser-api) | the Russian exit, a managed browser and `Captcha.setAutoSolve` in one; the route every engine run above used |
| [Captcha solving](https://2captcha.com/api-docs/normal-captcha) | the image on every full record — required for `--mode cadastral`, $0.001 a record as measured |
| [Proxies](https://2captcha.com/proxy) (2prx.com) | a Russian exit for your own local browser (`--local`) |
| [Scraper API](https://2captcha.com/scraper/scraper-api/api) | the free address search and dictionaries without any browser |
| [Fingerprints](https://2captcha.com/fingerprints/api) | a consistent identity for a local browser (`--fingerprint`); not measured on this site — nothing here required it |

What is NOT needed: a Gosuslugi account, and — for the address search and the
dictionaries — any captcha at all.

## Monitoring

```bash
python3 playwright_scraper.py --input watchlist.txt --out "objects_$(date +%F)"
python3 diff_runs.py --old objects_2026-09-01.json --new objects_2026-10-01.json --fail-on-change
```

`diff_runs.py` reports re-valuations, status changes (an object
deregistered), and rights or encumbrances registered or lifted. It refuses
to compare a partial run, two runs of different inputs or modes, or a data
file that does not match its sidecar — each would report artefacts as
changes.

## Development

```bash
python3 smoke_test.py          # offline, no network, no key, no browser
python3 smoke_test.py --e2e    # + every installed engine, end to end, against tests/mock_site.py
```

`tests/mock_site.py` is a local stand-in built from the site's measured
behaviour — the real form ids, the blob-URL captcha image, the one-use
answer, the real answer bodies — so each engine is driven with a real browser
on every commit. See [CONTRIBUTING.md](CONTRIBUTING.md) and
[SECURITY.md](SECURITY.md).

Not implemented, and said so: the page's other three search types (by
restriction-of-right number, by previously assigned number, by right number)
— their request bodies were not observed, and this repo does not guess one.

## Legal

This tool reads a public government reference service the way its own page
does. You are responsible for how you use it: the service's terms, the
volume you send, and data-protection law where you are — the records are
about property, but addresses and rights can relate to people. The
cadastral engineer's personal data is deliberately not collected.

MIT licence.
