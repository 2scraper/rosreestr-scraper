#!/usr/bin/env python3
"""
puppeteer_scraper.py — the pyppeteer engine.

The same lookups as playwright_scraper.py, driven through pyppeteer. Kept for
parity, not because it is better: pyppeteer is effectively unmaintained and
its own README points at Playwright. Use it when you already have it, or to
check that a result is not an artefact of one driver. The policy is shared
(lookup_flow.py, output_writer.py), so the three engines agree on exit codes,
on what a captcha costs and on every row.

    python3 puppeteer_scraper.py --cad-number 77:01:0001044:3030

Engine-specific notes:

  * pyppeteer is async; lookup_flow is not. This engine owns ONE event loop
    and runs every call on it (a sync facade), rather than making the shared
    flow async for one driver's sake.
  * Proxy credentials never reach argv: the host goes into --proxy-server,
    the login through the DevTools Fetch domain (see _authenticate_proxy —
    pyppeteer's own page.authenticate() no longer works on current Chrome).
  * Its bundled Chromium is from 2018 and dies on a current macOS. Set
    PYPPETEER_EXECUTABLE_PATH to a browser that runs — the Chromium
    `playwright install chromium` fetched will do.
  * connect() has no timeout of its own; it gets one here.
  * The React form is driven by real typing (click, select-all, type), so
    the page's own onChange runs and fires its own captcha check.
"""

import asyncio
import logging
import os
import sys
import time

from pyppeteer import connect, launch
from pyppeteer.errors import NetworkError, PageError

import captcha_solver
import cli
import rosreestr_api as ra
from proxy_pool import (CDP_CONNECT_ATTEMPTS, CDP_CONNECT_PAUSE_S, CDP_CONNECT_TIMEOUT_S,
                        cdp_refusal_advice, cdp_retryable, mask, redact_secret_patterns,
                        to_pyppeteer)

logger = logging.getLogger("puppeteer_scraper")

NAV_TIMEOUT_MS = 60000
FORM_TIMEOUT_S = 30.0

# pyppeteer's evaluateOnNewDocument wraps its argument as `(<fn>)()`, so the
# hook — a plain script — goes in as the body of a function.
_HOOK_AS_FUNCTION = "function () {\n" + ra.NETWORK_HOOK_JS + "\n}"

_ENABLED_JS = "(sel) => { const b = document.querySelector(sel); return !!b && !b.disabled; }"
_VALUE_JS = "(sel) => { const e = document.querySelector(sel); return e ? e.value : ''; }"
_CLICK_TEXT_JS = """(text) => {
  for (const b of document.querySelectorAll('button, a, span')) {
    if ((b.textContent || '').indexOf(text) !== -1) { (b.closest('button') || b).click(); return true; }
  }
  return false;
}"""
# The captcha answer goes in the way a paste does: the native value setter
# (so React sees a real change) and ONE input event. Typed key by key, the
# page checks every prefix — /captcha/д, /captcha/ду, … — and the verdicts
# come back out of order: a late 403 on a prefix disabled the search button
# after the full answer had been accepted (found by tests/engine_e2e.py).
_PASTE_JS = """(sel, value) => {
  const el = document.querySelector(sel);
  const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
  el.focus(); set.call(el, value);
  el.dispatchEvent(new Event('input', {bubbles: true}));
  el.dispatchEvent(new Event('change', {bubbles: true}));
}"""
# Bounded like the Selenium twin's script timeout (30 s).
_FETCH_JS = """async (url) => {
  const c = new AbortController();
  const t = setTimeout(() => c.abort(), 30000);
  try {
    const r = await fetch(url, {credentials: 'same-origin', signal: c.signal});
    return {s: r.status, b: await r.text()};
  } catch (e) {
    return {s: null, b: null};
  } finally {
    clearTimeout(t);
  }
}"""


def _quiet_refused_handshakes(loop):
    """Drop the orphaned-task traceback a refused CDP handshake leaves behind."""
    default = loop.get_exception_handler()

    def handler(lp, context):
        exc = context.get("exception")
        if exc is not None and type(exc).__name__ in ("InvalidStatusCode", "InvalidStatus",
                                                       "InvalidHandshake"):
            logger.debug("Ignored a refused-handshake task: %s", exc)
            return
        if default is not None:
            default(lp, context)
        else:
            lp.default_exception_handler(context)

    loop.set_exception_handler(handler)


class PuppeteerSession:
    """lookup_flow's session contract over pyppeteer, synchronously."""

    def __init__(self, args, pool):
        self.args, self.pool = args, pool
        self.autosolve = False
        self.autosolve_events = []
        self._loop = asyncio.new_event_loop()
        self._run(self._open())

    def _run(self, coro):
        return self._loop.run_until_complete(coro)

    async def _open(self):
        a = self.args
        if a.cdp_endpoint:
            logger.info("Connecting to the Scraping Browser: %s", mask(a.cdp_endpoint))
            # pyppeteer never surfaces a refused handshake (the 500 dies in its
            # receive loop): a short per-attempt timeout, proxy_pool's retry
            # policy, and the orphaned-task traceback filtered (template §26).
            _quiet_refused_handshakes(self._loop)
            self.browser = None
            for attempt in range(1, CDP_CONNECT_ATTEMPTS + 1):
                try:
                    self.browser = await asyncio.wait_for(
                        connect(browserWSEndpoint=a.cdp_endpoint), timeout=CDP_CONNECT_TIMEOUT_S)
                    break
                except Exception as e:  # noqa: BLE001 — re-raised redacted
                    text = redact_secret_patterns(f"{type(e).__name__}: {e}")[:300]
                    if attempt < CDP_CONNECT_ATTEMPTS and cdp_retryable(text):
                        logger.warning("Scraping Browser connect failed (%s); retrying in "
                                       "%.0fs [%d/%d].", text, CDP_CONNECT_PAUSE_S, attempt,
                                       CDP_CONNECT_ATTEMPTS)
                        await asyncio.sleep(CDP_CONNECT_PAUSE_S)
                        continue
                    raise RuntimeError(f"Could not connect to --cdp-endpoint: {text} — "
                                       f"{cdp_refusal_advice(text)}") from None
            self._remote = True
            self.page = await self.browser.newPage()
            await self.page.evaluateOnNewDocument(_HOOK_AS_FUNCTION)
            try:
                cdp = await self.page.target.createCDPSession()
                await cdp.send("Captcha.setAutoSolve",
                               {"autoSolve": True, "options": [{"type": "*"}]})
                for ev in ("detected", "waitForSolve", "solveFinished", "solveFailed"):
                    cdp.on(f"Captcha.{ev}", lambda payload=None, ev=ev: self._event(ev, payload))
                self._cdp = cdp
                self.autosolve = True
                logger.info("Scraping Browser Captcha.setAutoSolve enabled.")
            except Exception as e:  # noqa: BLE001
                logger.info("Captcha.setAutoSolve is not available here (%s).",
                            redact_secret_patterns(str(e))[:200])
            return
        launch_args = ["--no-sandbox", "--disable-dev-shm-usage", f"--lang={a.locale}"]
        creds = None
        if self.pool:
            proxy_arg, creds = to_pyppeteer(self.pool.current)
            if proxy_arg:
                launch_args.append(proxy_arg)
                logger.info("Using proxy exit %s", mask(self.pool.current))
        kw = {"headless": a.headless, "args": launch_args}
        exe = os.environ.get("PYPPETEER_EXECUTABLE_PATH")
        if exe:
            kw["executablePath"] = exe
        self.browser = await launch(**kw)
        self._remote = False
        self.page = await self.browser.newPage()
        if creds:
            await self._authenticate_proxy(creds)
        await self.page.evaluateOnNewDocument(_HOOK_AS_FUNCTION)
        if a.fingerprint:
            from fingerprint_client import cdp_identity_commands, get_fingerprint
            fp = get_fingerprint(a.twocaptcha_key, tags=a.fp_tags, country=a.fp_country)
            cdp = await self.page.target.createCDPSession()
            for method, params in cdp_identity_commands(fp):
                await cdp.send(method, params)
            logger.info("Using 2captcha fingerprint %s (%s)", fp.get("id"), fp.get("country"))

    async def _authenticate_proxy(self, creds):
        """Answer the proxy's 407 through the CDP Fetch domain.

        Not page.authenticate(): pyppeteer implements it with
        Network.setRequestInterception, which current Chrome no longer has —
        measured 2026-09-29 against Chrome for Testing: "Protocol error
        (Network.setRequestInterception): ... wasn't found", exit 5 before
        the first request. Fetch.authRequired is the supported way, and the
        password still travels over the DevTools protocol, never argv.
        """
        cdp = await self.page.target.createCDPSession()

        def paused(event):
            asyncio.ensure_future(cdp.send("Fetch.continueRequest",
                                           {"requestId": event["requestId"]}))

        def auth(event):
            asyncio.ensure_future(cdp.send("Fetch.continueWithAuth", {
                "requestId": event["requestId"],
                "authChallengeResponse": {"response": "ProvideCredentials",
                                          "username": creds["username"],
                                          "password": creds["password"]}}))

        cdp.on("Fetch.requestPaused", paused)
        cdp.on("Fetch.authRequired", auth)
        await cdp.send("Fetch.enable", {"handleAuthRequests": True,
                                        "patterns": [{"urlPattern": "*"}]})
        self._auth_cdp = cdp

    def _event(self, name, payload):
        self.autosolve_events.append(f"Captcha.{name}")
        logger.info("[Scraping Browser] Captcha.%s %s", name, str(payload)[:160])

    # --- navigation --------------------------------------------------------
    def goto(self, url):
        async def go():
            resp = await self.page.goto(url, {"waitUntil": "domcontentloaded",
                                              "timeout": NAV_TIMEOUT_MS})
            await asyncio.sleep(1.5)
            return resp.status if resp else None
        status = self._run(go())
        return status, self.html()

    def open_service(self):
        status, _ = self.goto(self.args.url)
        deadline = time.monotonic() + FORM_TIMEOUT_S
        while time.monotonic() < deadline:
            if self._run(self.page.querySelector(ra.SEL_SEARCH_BUTTON)):
                break
            self.wait(0.4)
        return status, self.html()

    def html(self):
        for _ in range(4):
            try:
                return self._run(self.page.content())
            except (NetworkError, PageError):
                self.wait(0.6)
        return ""

    # --- the form ----------------------------------------------------------
    def net_entries(self):
        try:
            return self._run(self.page.evaluate("() => window.__rrNet || []")) or []
        except (NetworkError, PageError):
            return []

    def captcha_value(self):
        return self._run(self.page.evaluate(_VALUE_JS, ra.SEL_CAPTCHA_INPUT)) or ""

    def captcha_image(self):
        async def shot():
            for sel in ra.SEL_CAPTCHA_IMAGE.split(","):
                el = await self.page.querySelector(sel.strip())
                if el:
                    return await el.screenshot()
            raise RuntimeError(f"no captcha image matches {ra.SEL_CAPTCHA_IMAGE!r}")
        return self._run(shot())

    def _type(self, selector, text):
        async def typ():
            # Select through the element, not a shortcut: Ctrl+A is "start of
            # line" on macOS and would leave the old text in place.
            await self.page.focus(selector)
            await self.page.evaluate("(s) => document.querySelector(s).select()", selector)
            await self.page.keyboard.press("Backspace")
            if text:
                await self.page.type(selector, text, {"delay": 40})
        self._run(typ())

    def set_captcha(self, text):
        self._run(self.page.evaluate(_PASTE_JS, ra.SEL_CAPTCHA_INPUT, text))

    def refresh_captcha(self):
        self._run(self.page.evaluate(_CLICK_TEXT_JS, ra.CAPTCHA_REFRESH_TEXT))

    def set_query(self, text):
        if self._run(self.page.evaluate(_VALUE_JS, ra.SEL_QUERY)) != text:
            self._type(ra.SEL_QUERY, text)

    def search_enabled(self):
        return bool(self._run(self.page.evaluate(_ENABLED_JS, ra.SEL_SEARCH_BUTTON)))

    def click_search(self):
        self._run(self.page.click(ra.SEL_SEARCH_BUTTON))

    def fetch_text(self, url):
        res = self._run(self.page.evaluate(_FETCH_JS, url)) or {}
        return res.get("s"), res.get("b")

    def runtime_captcha_info(self):
        try:
            return self._run(self.page.evaluate(captcha_solver.CAPTCHA_DISCOVERY_JS))
        except (NetworkError, PageError) as e:
            logger.warning("In-page captcha discovery failed: %s", e)
            return None

    def inject_token(self, token):
        self._run(self.page.evaluate(captcha_solver.INJECT_TOKEN_FN, token))

    def wait(self, seconds):
        self._run(asyncio.sleep(seconds))

    def dump(self, directory, tag):
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, f"{tag}.html"), "w", encoding="utf-8") as f:
            f.write(self.html())
        try:
            self._run(self.page.screenshot({"path": os.path.join(directory, f"{tag}.png"),
                                            "fullPage": True}))
        except (NetworkError, PageError) as e:
            logger.debug("screenshot failed: %s", e)

    def close(self):
        try:
            if self._remote:
                self._run(self.page.close())
                self._run(self.browser.disconnect())
            else:
                self._run(self.browser.close())
        except Exception as e:  # noqa: BLE001
            logger.debug("teardown: %s", e)
        finally:
            self._loop.close()


def open_session(args, pool):
    return PuppeteerSession(args, pool)


def main(argv=None) -> int:
    return cli.main("puppeteer", __doc__, open_session, argv)


if __name__ == "__main__":
    sys.exit(main())
