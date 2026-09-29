# Troubleshooting

Every entry here was met in a real run on 2026-09-29 unless it says
otherwise. Start with `python3 env_config.py`: it says which settings were
picked up from `.env`, without printing any of them.

## "The service page did not load" — exit 5

    lk.rosreestr.ru does not answer from outside Russia ...

**Cause:** the exit is not in Russia. From a Belgian address every Rosreestr
host timed out at the TCP connect — no refusal page, no status, just silence.
Through the Scraper API's own exits, a tiny request (`/account-back/config`)
answered, while the address search timed out on both attempts — so do not
rely on its exits either.

**Fix:** a Russian exit, either of:

- `ROSREESTR_CDP_ENDPOINT` — a Scraping Browser profile with `country-ru`;
- `ROSREESTR_PROXY` — a 2Captcha proxy login with `-region-ru`, used with
  `--local` (or on its own, with no endpoint set).

## "Could not connect to --cdp-endpoint … HTTP 500"

The Scraping Browser refused the WebSocket upgrade. Seen on every connect
from 12:15 to 12:17 (local time) on 2026-09-29, starting right after a run on
the same profile had ended; the next attempt, at 12:27, succeeded.
One profile (`pid-`) holds one live connection. A sibling repo measured the
lock lasting 1.6–1.9 s after a clean disconnect (family template §26); here
the refusals went on for minutes, so a short retry does not always ride it out. The engines try 3 times, 3 s apart; a
401 (expired credentials) is not retried. If it persists, wait a few minutes or use another `pid`. The profile's
credentials also expire (about a day) — take fresh ones from the dashboard.

## "Loaded … but it is not the search form" — exit 5

The page answered but the form did not appear within 30 s. Seen once through
a residential proxy (`--local`); the same command succeeded on the next run.
The page as it was is saved beside `--out` as `<out>.debug.html` / `.png`.
If that page is the browser's own error page, the proxy failed — see below.

## Selenium + a proxy with a login: "Selenium cannot authenticate a proxy"

A limit of Chrome itself, not of this repo: `--proxy-server` has nowhere to
put a password, so the credentials are dropped and the proxy refuses the
connection (measured: the page had no status and no title, the form never
appeared, exit 5). Use the Playwright or
pyppeteer engine, or an IP-allowlisted exit. Selenium also cannot use the
Scraping Browser (chromedriver cannot authenticate a WebSocket endpoint), so
its captchas are always solved through 2Captcha.

## "The site rejected the answer … (captcha check answered 403)"

Normal at a low rate: of 16 image solves on 2026-09-29 (7 in hand-run
probes, 9 in engine runs) the site refused 3. The wrong answer is reported back
(`reportIncorrect`), a fresh image is solved, up to `--captcha-attempts`.
Why a refused answer was refused is not known: one refused answer matched
the image as a person reads it (after a 61.5 s solve), while another solve of
61.4 s was accepted — so the time taken is not the explanation.

If EVERY answer is rejected, the run ends with exit 3 and writes nothing.

## Auto-solve "did not answer … and sent no Captcha.* event"

Expected on this site. `Captcha.setAutoSolve` is enabled on every Scraping
Browser session and gets the first turn on every captcha, but it has not
answered this image captcha once (0 events on 2026-09-29). After two silent
full waits it gets 3 s per captcha instead of `--autosolve-wait`.
`--autosolve-wait 0` skips it entirely.

## Exit 4 — every query answered, nothing matched

`{"elements":[],"count":0}` for every number: the registry has no such
object. Nothing is written, so a previous good output is not overwritten;
`--allow-empty` writes an empty table instead. The numbers are listed in the
sidecar's `queries_not_found`.

## The address search returned exactly 100 objects

That is the site's cap, and the 100 are a SAMPLE that changes between calls:
two calls 25 s apart returned 100 lines each with only 94 in common, in a
different order. The sidecar lists such queries in `address_capped`. Narrow
the address itself (house, building, room number): the site offers no other
filter. It ignores every `objType` value it is sent (measured: the same 100
mixed lines for `PARCEL`, `OKS`, `FLAT` and the object-type codes), so
`--list-kind` filters on this side, after the site has chosen its 100.

## "Budget exhausted"

`--max-solves` (default: queries × `--captcha-attempts`) is a hard cap on paid
solves for the run. Raise it deliberately; it exists so a site change cannot
turn into an open-ended bill.

## pyppeteer: "Browser closed unexpectedly"

Its bundled Chromium is from 2018. Point it at a current one:

    export PYPPETEER_EXECUTABLE_PATH="$(python3 -c 'from playwright.sync_api import sync_playwright as s; p=s().start(); print(p.chromium.executable_path); p.stop()')"
