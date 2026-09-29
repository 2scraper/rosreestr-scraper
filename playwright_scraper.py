#!/usr/bin/env python3
"""
playwright_scraper.py — the primary engine.

Looks real-estate objects up in Rosreestr's online reference service
(https://lk.rosreestr.ru/eservices/real-estate-objects-online), the way the
page itself does: type the number, answer the image captcha, press "Найти",
read the answer the page receives. Every captcha gets the Scraping Browser's
auto-solve first (Captcha.setAutoSolve) and 2Captcha's ImageToTextTask second.

    # full records for two cadastral numbers
    python3 playwright_scraper.py --cad-number 77:01:0001044:3030 \\
        --cad-number 77:01:0001044:2981

    # every object at an address — free, no captcha
    python3 playwright_scraper.py --mode address --address "Москва, ул. Тверская, д. 13"

    # the nine object kinds and the other code dictionaries
    python3 playwright_scraper.py --mode dictionaries --out codes

A Russian exit is required: from outside Russia every Rosreestr host timed out
at the TCP connect (2026-09-29). Put ROSREESTR_CDP_ENDPOINT (country-ru) or
ROSREESTR_PROXY (-region-ru) in .env.

The policy — when to pay for a solve, what a 406 means, the exit codes — is
in lookup_flow.py and output_writer.py, shared with the other two engines.
This file only knows how to drive Playwright.
"""

import logging
import os
import sys
import time

from playwright.sync_api import Error as PWError, sync_playwright

import captcha_solver
import cli
import rosreestr_api as ra
from proxy_pool import (CDP_CONNECT_ATTEMPTS, CDP_CONNECT_PAUSE_S, cdp_refusal_advice,
                        cdp_retryable, mask, redact_secret_patterns, to_playwright)

logger = logging.getLogger("playwright_scraper")

NAV_TIMEOUT_MS = 60000
FORM_TIMEOUT_S = 30.0


class PlaywrightSession:
    """One browser + context + page, implementing lookup_flow's contract."""

    def __init__(self, args, pool):
        self.args, self.pool = args, pool
        self.autosolve = False
        self.autosolve_events = []
        self._pw = sync_playwright().start()
        try:
            if args.cdp_endpoint:
                self._connect_remote()
            else:
                self._launch_local()
        except BaseException:
            self._pw.stop()
            raise
        self.page.set_default_timeout(NAV_TIMEOUT_MS)

    # --- setup -------------------------------------------------------------
    def _launch_local(self):
        a = self.args
        launch = {"headless": a.headless}
        proxy = to_playwright(self.pool.current) if self.pool else None
        if proxy:
            launch["proxy"] = proxy
            logger.info("Using proxy exit %s", mask(self.pool.current))
        self.browser = self._pw.chromium.launch(**launch)
        # No hardcoded user agent: the browser's own is the one its JS engine
        # and TLS handshake agree with.
        ctx = {"locale": a.locale}
        init = None
        if a.fingerprint:
            from fingerprint_client import (get_fingerprint, playwright_context_kwargs,
                                            playwright_init_script)
            fp = get_fingerprint(a.twocaptcha_key, tags=a.fp_tags, country=a.fp_country)
            ctx.update(playwright_context_kwargs(fp))
            init = playwright_init_script(fp)
            logger.info("Using 2captcha fingerprint %s (%s)", fp.get("id"), fp.get("country"))
        self.context = self.browser.new_context(**ctx)
        self.context.add_init_script(ra.NETWORK_HOOK_JS)
        if init:
            self.context.add_init_script(init)
        self.page = self.context.new_page()
        self._remote = False

    def _connect_remote(self):
        logger.info("Connecting to the Scraping Browser: %s", mask(self.args.cdp_endpoint))
        # Wrapped: Playwright repeats the endpoint, password included, five
        # times in one connect error. An exception message is a log.
        # proxy_pool's CDP policy, shared with every engine in the family: a
        # profile stays locked 1.6-1.9 s after a clean disconnect (measured in a
        # sibling repo, template §26), so a lock is retried; an expired login
        # (401) is not. Longer outages were seen here; the retry does not hide them.
        self.browser = None
        for attempt in range(1, CDP_CONNECT_ATTEMPTS + 1):
            try:
                self.browser = self._pw.chromium.connect_over_cdp(self.args.cdp_endpoint,
                                                                  timeout=60000)
                break
            except Exception as e:  # noqa: BLE001 — re-raised redacted
                text = " ".join(redact_secret_patterns(str(e)).split())[:600]
                if attempt < CDP_CONNECT_ATTEMPTS and cdp_retryable(text):
                    logger.warning("Scraping Browser connect failed (%s); retrying in %.0fs "
                                   "[%d/%d].", text, CDP_CONNECT_PAUSE_S, attempt,
                                   CDP_CONNECT_ATTEMPTS)
                    time.sleep(CDP_CONNECT_PAUSE_S)
                    continue
                raise RuntimeError(f"Could not connect to --cdp-endpoint: {text} — "
                                   f"{cdp_refusal_advice(text)}") from None
        self.context = self.browser.contexts[0] if self.browser.contexts else self.browser.new_context()
        self.context.add_init_script(ra.NETWORK_HOOK_JS)
        self.page = self.context.new_page()
        self._remote = True
        # https://2captcha.com/scraper/browser-api — solve captchas inside
        # the browser. Captcha.solveFinished is the success signal.
        try:
            cdp = self.context.new_cdp_session(self.page)
            cdp.send("Captcha.setAutoSolve", {"autoSolve": True, "options": [{"type": "*"}]})
            for ev in ("detected", "waitForSolve", "solveFinished", "solveFailed"):
                cdp.on(f"Captcha.{ev}", lambda payload, ev=ev: self._autosolve_event(ev, payload))
            self.autosolve = True
            self._cdp = cdp
            logger.info("Scraping Browser Captcha.setAutoSolve enabled.")
        except Exception as e:  # noqa: BLE001 — a non-2Captcha CDP endpoint
            logger.info("Captcha.setAutoSolve is not available here (%s); "
                        "2Captcha will solve instead.", redact_secret_patterns(str(e))[:200])

    def _autosolve_event(self, name, payload):
        self.autosolve_events.append(f"Captcha.{name}")
        logger.info("[Scraping Browser] Captcha.%s %s", name, str(payload)[:160])

    # --- navigation --------------------------------------------------------
    def goto(self, url):
        resp = self.page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        self.page.wait_for_timeout(1500)
        return (resp.status if resp else None), self.html()

    def open_service(self):
        status, _ = self.goto(self.args.url)
        deadline = time.monotonic() + FORM_TIMEOUT_S
        # Polled through the protocol, never wait_for_function: an evaluated
        # string is what a strict CSP forbids (family §18).
        while time.monotonic() < deadline:
            if self.page.locator(ra.SEL_SEARCH_BUTTON).count():
                break
            self.page.wait_for_timeout(400)
        return status, self.html()

    def html(self):
        for _ in range(4):
            try:
                return self.page.content()
            except PWError as e:
                if "navigating" not in str(e).lower():
                    raise
                self.page.wait_for_timeout(600)
        return ""

    # --- the form ----------------------------------------------------------
    def net_entries(self):
        try:
            return self.page.evaluate("() => window.__rrNet || []")
        except PWError:
            return []

    def captcha_value(self):
        return self.page.locator(ra.SEL_CAPTCHA_INPUT).input_value()

    def captcha_image(self):
        return self.page.locator(ra.SEL_CAPTCHA_IMAGE).first.screenshot(timeout=10000)

    def set_captcha(self, text):
        self.page.locator(ra.SEL_CAPTCHA_INPUT).fill(text)

    def refresh_captcha(self):
        self.page.get_by_text(ra.CAPTCHA_REFRESH_TEXT).first.click()

    def set_query(self, text):
        box = self.page.locator(ra.SEL_QUERY)
        if box.input_value() != text:
            box.fill(text)

    def search_enabled(self):
        return self.page.locator(ra.SEL_SEARCH_BUTTON).is_enabled()

    def click_search(self):
        # force: a disabled button is a no-op click, as in the other two
        # drivers, rather than a 10 s wait and an exception (engine parity).
        self.page.locator(ra.SEL_SEARCH_BUTTON).click(timeout=10000, force=True)

    def fetch_text(self, url):
        # Bounded like the Selenium twin's script timeout: a stalled fetch
        # would otherwise wait on Chrome's own network timeout.
        res = self.page.evaluate(
            "async (url) => { const c = new AbortController();"
            " const t = setTimeout(() => c.abort(), 30000);"
            " try { const r = await fetch(url, {credentials: 'same-origin', signal: c.signal});"
            " return {s: r.status, b: await r.text()}; }"
            " catch (e) { return {s: null, b: null}; } finally { clearTimeout(t); } }", url)
        return res.get("s"), res.get("b")

    def runtime_captcha_info(self):
        try:
            return self.page.evaluate(captcha_solver.CAPTCHA_DISCOVERY_JS)
        except PWError as e:
            logger.warning("In-page captcha discovery failed: %s", e)
            return None

    def inject_token(self, token):
        self.page.evaluate(captcha_solver.INJECT_TOKEN_FN, token)

    def wait(self, seconds):
        self.page.wait_for_timeout(int(seconds * 1000))

    def dump(self, directory, tag):
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, f"{tag}.html"), "w", encoding="utf-8") as f:
            f.write(self.html())
        try:
            self.page.screenshot(path=os.path.join(directory, f"{tag}.png"), full_page=True)
        except PWError as e:
            logger.debug("screenshot failed: %s", e)

    def close(self):
        try:
            if self._remote:
                self.page.close()        # leave the remote browser to its owner
            else:
                self.browser.close()
        except Exception as e:  # noqa: BLE001
            logger.debug("teardown: %s", e)
        finally:
            self._pw.stop()


def open_session(args, pool):
    return PlaywrightSession(args, pool)


def main(argv=None) -> int:
    return cli.main("playwright", __doc__, open_session, argv)


if __name__ == "__main__":
    sys.exit(main())
