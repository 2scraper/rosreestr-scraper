# Contributing

Bug reports, site-change reports and pull requests are all welcome. This file
covers the few things specific to a scraper, which are not the usual ones.

## Before you open anything

Run the offline suite. It needs no network, no browser and no API key, and takes
about a second:

```bash
pip install -r requirements.txt
python3 smoke_test.py
```

It prints its own `passed / failed / skipped` counts, and lists both the
individual checks it had to skip and any group skipped because an engine
library is absent. A check whose input is missing must call `skip()`, never
`check(label, True)` — a skip asserted as a pass is indistinguishable in the
output from a check that actually ran, and it inflates the count the README
used to quote.

The fixtures are `fixtures.json` — real `lk.rosreestr.ru` answers — and
`rosreestr_codes.py`, the site's own code dictionaries (a generated module). Both are cut by
`make_fixtures.py` from a live probe log that is NOT in the repository: a
full record carries the cadastral engineer's name and phone (a private
person), and `make_fixtures.py` replaces them with `{scrubbed}` before
anything is written. To refresh them, capture a run's traffic with
`--dump-html DIR` (it writes `queryN.net.json` beside each page) and point
`make_fixtures.py` at a log of the same shape.

`python3 smoke_test.py --e2e` additionally drives every INSTALLED engine,
with a real local browser, against `tests/mock_site.py` — a local stand-in
built from the site's measured behaviour. CI runs that per engine.

**The suite must pass with no engine installed at all.** CI's offline job installs
only `requests`, so any import of `playwright_scraper`,
`puppeteer_scraper` or `selenium_scraper` in a test has to sit inside
`try/except ImportError` with the skip recorded. This is easy to get wrong
locally, where you almost certainly have an engine installed and an unguarded
import passes.

If the suite fails on a clean clone, that is itself the bug — say so.

## Dependencies: `.txt` is the spec, `.lock` is what gets installed

`requirements*.txt` hold the loose `>=` floors a person edits. Each has a
`.lock` beside it — exact versions and hashes, resolved for every Python from
3.9 up — and CI, the canary and the Docker image install only the locks, with
`--require-hashes`. `smoke_test.py` fails if a workflow installs anything
else, if a lock no longer satisfies its `.txt`, or if an action is referenced
by tag instead of by commit SHA.

After changing a `.txt`, or when `audit.yml` reports an advisory, regenerate
the locks with [uv](https://docs.astral.sh/uv/) — the exact command is also in
each lock's header:

```bash
uv pip compile --universal --python-version 3.9 --generate-hashes --annotation-style line \
    requirements.txt -o requirements.lock
uv pip compile --universal --python-version 3.9 --generate-hashes --annotation-style line \
    requirements.txt requirements-playwright.txt -o requirements-playwright.lock
uv pip compile --universal --python-version 3.9 --generate-hashes --annotation-style line \
    requirements.txt requirements-puppeteer.txt -o requirements-puppeteer.lock
uv pip compile --universal --python-version 3.9 --generate-hashes --annotation-style line \
    requirements.txt requirements-selenium.txt -o requirements-selenium.lock
uv pip compile --universal --python-version 3.9 --generate-hashes --annotation-style line \
    requirements.txt .github/requirements-ci.txt -o .github/requirements-ci.lock
```

Actions are pinned as `uses: owner/action@<40-char SHA> # vX.Y.Z`; Dependabot
(`.github/dependabot.yml`) proposes updates to both together.

## Never commit a credential

`.env` is in `.gitignore`. Keep it there.

The scrapers mask `user:pass@` in their own log lines (and the Scraper API's
`x-debug` header before logging it), but two things are **not** masked: raw
HTML dumps and your shell history. Before pasting any output into an issue or a PR, replace keys,
proxy passwords and full `ws://user:pass@host:9222` endpoints with `***`.

CI fails the build if something that looks like a credential is committed. That
check is a backstop, not a review — a leaked key has to be rotated whether or
not the check caught it.

## Reporting a site change

Rosreestr changing its page or API is the normal way this stops working, and
it has its own issue template. What this code relies on, so a report can say
which part moved:

- the form's ids: `#query`, `#captcha`, `#realestateobjects-search`, and the
  captcha image `<img alt="captcha">` (its `src` is a `blob:` URL);
- `GET /account-back/captcha.png`, `GET /account-back/captcha/{text}`
  (200 accepted / 403 wrong), `POST /account-back/on` with
  `{"filterType":"cadastral","cadNumbers":[…],"captcha":"…"}`;
- `GET /account-back/address/search?term=…&objType=all` for the free list;
- `GET /account-back/dictionary/{NAME}?sortKey=code` for the codes.

`--dump-html DIR` writes the page, a screenshot and the page's own
`/account-back/` traffic for every query, on success as well as failure.

## Pull requests

**Add a test for the behaviour you are changing.** `smoke_test.py` is a single
file of plain functions reading `fixtures.json` — no pytest, no
conftest. Copy the nearest existing check and edit it. And **break your fix
once on purpose** and watch the suite go red through the check you meant: a
guard that cannot fail passes a green suite perfectly.

Properties in this repo exist because they were once absent somewhere in this
family and cost real time. Tests pin them, so a PR that breaks one fails
rather than silently regressing:

- **One captcha buys one record, and the counter is the bill.** Every paid
  solve goes through one budget (`lookup_flow.Budget`), `--max-solves` is a
  hard cap, and a wrong answer is reported back to 2Captcha.
- **The Scraping Browser's auto-solve gets the first turn** on every captcha,
  2Captcha the second — the brief's requirement, pinned by the suite.
- **Only the check of the WHOLE answer is the verdict.** A driver that types
  key by key makes the page check every prefix; those 403s are not refusals.
- **A run that finds nothing writes nothing.** It must not replace a good
  output file with `[]`. `--allow-empty` is the opt-out.
- **Exit codes are a contract**: `0` ok, `1` crash, `2` bad usage, `3`
  blocked (a captcha the site keeps refusing, a refusal), `4` every query
  answered "no such object", `5` nothing was obtained, `6` partial. Every
  engine produces the same code for the same situation because they share
  `lookup_flow` and `finish_run`.
- **"Not found" is an answer, not a failure.** It is listed in the sidecar's
  `queries_not_found`, separately from `queries_failed`.
- **No personal data in the output.** The cadastral engineer's name, phone
  and certificate number are never read into a row.

There is also a naming check: certain phrases are banned repo-wide and the suite
fails naming them. If it trips, read the message — the phrase is wrong for a
reason, not merely unfashionable.

### Style

- **Match the file you are editing.** No formatter is enforced.
- **Comments explain *why*.** What the code does is visible; why it does it that
  way, especially where the obvious version is wrong, is not.
- **A timeout on every remote call.** Every browser library used here has needed
  an explicit timeout its own API does not provide, and each has needed its own
  route out of the runtime — reporting a timeout is not the same as exiting on
  one. If you add a call to a remote browser or API, bound it.
- **Fail loudly.** A function that returns an empty list on error, or logs
  success without checking that the thing it wanted actually happened, is the
  single most common bug class in this codebase's history. A selector that
  matches the *wrong* element is worse than one that matches nothing, because
  the second one tells you.

### If your change needs a live run

Most do not — the suite covers the parser, the flow, the writers and every
engine against the mock. If yours genuinely needs the live site, say in the
PR what you ran, through which Russian exit (a Scraping Browser profile or a
proxy), how many captchas it bought, and what you got. Compare two runs by
cadastral number, never by position.

Do not add anything that logs in to Gosuslugi. This project reads the public
online reference service the way its own page does, and nothing else.

## Scope

This repo reads Rosreestr's public online reference service
(`lk.rosreestr.ru/eservices/real-estate-objects-online`): object records by
cadastral number, the free address search and the code dictionaries. Out of
scope: anything behind an ESIA login, ordering EGRN extracts, and the
page's three other search types until someone measures their request bodies.

## Licence

MIT. By opening a pull request you agree your contribution ships under it.
