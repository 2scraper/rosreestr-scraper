#!/usr/bin/env python3
"""
scraper_api_client.py — the 2Captcha Scraper API edition (no local browser).

The FREE half of this repo — the address search and the code dictionaries —
through 2Captcha's Scraper API (https://2captcha.com/scraper/scraper-api/api):
one POST per request, JSON back, no browser, no driver, only
requirements.txt installed.

    python3 scraper_api_client.py --mode address --address "Москва, ул. Тверская, д. 13"
    python3 scraper_api_client.py --mode dictionaries --out codes

What it cannot do, and why — a property of the two products, not a gap to
fill later: **full records (--mode cadastral) need a browser session.** A
record costs a one-use image captcha that belongs to the page session that
showed it; the Scraper API runs each request as its own task, so there is no
session to answer a captcha in. Use playwright_scraper.py for full records.

Reaching the site: lk.rosreestr.ru answers only Russian exits. The Scraper
API's own exits are not documented by country, so with ROSREESTR_CDP_ENDPOINT
set (a Scraping Browser profile with country-ru) the task is routed through
that browser via the API's `cdpurl` field. Measured: see the README.

API surface used
----------------
  POST https://scraper.2captcha.com/tasks/sync
    Authorization: Bearer <API_KEY>
    {"task_type": "scrape", "url": ..., "data_format": "raw", "format": "json",
     "timeout": 1..120, "cdpurl": "ws://..." (optional)}
  -> 200 {"status": "<API verdict>", "http_code": <target status>,
          "headers": {...}, "body": "<the target's body>"}

The key rides in a header, never in a URL, so no exception can carry it; the
`x-debug` response header echoes the task (a cdpurl password included) and is
redacted before it is logged.
"""

import logging
import sys
from urllib.parse import urlsplit

import requests

import cli
import output_writer as ow
from proxy_pool import mask, redact_secret_patterns

logger = logging.getLogger("scraper_api_client")

API_BASE = "https://scraper.2captcha.com"
SYNC_ENDPOINT = f"{API_BASE}/tasks/sync"
MAX_API_TIMEOUT = 120   # the API's own cap on `timeout`
TASK_TIMEOUT = 90


class ScraperApiSession:
    """The part of lookup_flow's session contract a one-shot fetch can serve."""

    # No form, no captcha: lookup_flow.open_or_explain checks reachability
    # with open_service() instead of looking for the search button.
    needs_form = False
    autosolve = False
    autosolve_events = []

    def __init__(self, args, pool):
        if not args.twocaptcha_key:
            raise RuntimeError("The Scraper API needs a 2Captcha key: set "
                               "TWOCAPTCHA_KEY in .env or pass --twocaptcha-key.")
        self.args = args
        parts = urlsplit(args.url)
        self.origin = f"{parts.scheme}://{parts.netloc}"
        if args.cdp_endpoint:
            logger.info("Routing Scraper API tasks through the Scraping Browser %s",
                        mask(args.cdp_endpoint))

    def _task(self, url):
        payload = {"task_type": "scrape", "url": url, "data_format": "raw",
                   "format": "json", "timeout": min(TASK_TIMEOUT, MAX_API_TIMEOUT)}
        if self.args.cdp_endpoint:
            payload["cdpurl"] = self.args.cdp_endpoint
        logger.info("Scraper API task: %s", url[:160])
        try:
            resp = requests.post(SYNC_ENDPOINT, json=payload, timeout=TASK_TIMEOUT + 30,
                                 headers={"Authorization": f"Bearer {self.args.twocaptcha_key}"})
        except requests.RequestException as e:
            raise RuntimeError("Could not reach the Scraper API: "
                               + redact_secret_patterns(str(e))) from None
        debug = resp.headers.get("x-debug")
        if debug:
            logger.debug("x-debug: %s", redact_secret_patterns(debug))
        if resp.status_code != 200:
            raise RuntimeError(f"Scraper API answered HTTP {resp.status_code}: "
                               + redact_secret_patterns(resp.text[:300]))
        data = resp.json()
        # The TARGET's status is http_code; `status` is the API's verdict.
        if data.get("status") == "error":
            logger.warning("Scraper API reports the target unreachable: %s",
                           redact_secret_patterns(str(data)[:300]))
        code = data.get("http_code")
        return (code if isinstance(code, int) else None), data.get("body")

    # --- the contract ------------------------------------------------------
    def open_service(self):
        return self._task(self.origin + "/account-back/config")

    def fetch_text(self, path_or_url):
        url = path_or_url if "://" in path_or_url else self.origin + path_or_url
        return self._task(url)

    def net_entries(self):
        return []

    def wait(self, seconds):
        import time
        time.sleep(seconds)

    def close(self):
        pass


def open_session(args, pool):
    return ScraperApiSession(args, pool)


def main(argv=None) -> int:
    args = cli.parse_args("scraper_api", __doc__, argv)
    if args.mode not in ("address", "dictionaries"):
        logger.error("--mode %s needs a browser session (a one-use captcha per "
                     "record, answered in the page that showed it). The Scraper "
                     "API serves --mode address and --mode dictionaries; use "
                     "playwright_scraper.py for %s.", args.mode, args.mode)
        return ow.EXIT_USAGE
    if args.details:
        logger.error("--details needs a browser session; drop it, or use "
                     "playwright_scraper.py --mode address --details.")
        return ow.EXIT_USAGE
    try:
        return cli.run(args, open_session, "scraper_api")
    except KeyboardInterrupt:
        return ow.EXIT_CRASH


if __name__ == "__main__":
    sys.exit(main())
