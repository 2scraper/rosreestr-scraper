#!/usr/bin/env python3
"""
selenium_scraper.py — the Selenium engine.

The same lookups as playwright_scraper.py, driven through Selenium and a
local Chrome. The policy is shared (lookup_flow.py, output_writer.py), so the
three engines agree on exit codes, on what a captcha costs and on every row.

    python3 selenium_scraper.py --cad-number 77:01:0001044:3030

Two limits of Selenium itself, stated here because a user would otherwise
discover them from a failed run:

  * **No Scraping Browser.** Its endpoint authenticates on the WebSocket
    upgrade (ws://user:pass@host); chromedriver's debuggerAddress takes a
    bare host:port with nowhere to put a password. So --cdp-endpoint is
    refused, and with it Captcha.setAutoSolve: this engine's captchas are
    always solved through 2Captcha.
  * **No authenticated proxy.** Chrome's --proxy-server switch cannot carry
    credentials; a user:pass proxy URL has them stripped with a warning, and
    an exit that requires them will refuse the connection. Use an
    IP-allowlisted exit, or the Playwright / pyppeteer engine.

The network hook goes in through CDP (Page.addScriptToEvaluateOnNewDocument),
which chromedriver exposes for a local Chrome.
"""

import logging
import os
import sys
import time

from selenium import webdriver
from selenium.common.exceptions import JavascriptException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

import captcha_solver
import cli
import rosreestr_api as ra
from proxy_pool import mask, to_selenium

logger = logging.getLogger("selenium_scraper")

NAV_TIMEOUT_S = 60
FORM_TIMEOUT_S = 30.0

# Selenium's execute_script takes a function BODY with an explicit return —
# the other two drivers take an arrow function. Written out per dialect.
_FETCH_BODY = """
var done = arguments[arguments.length - 1];
fetch(arguments[0], {credentials: 'same-origin'})
  .then(function (r) { return r.text().then(function (t) { done({s: r.status, b: t}); }); })
  .catch(function () { done({s: null, b: null}); });
"""
# The captcha answer goes in the way a paste does — see puppeteer_scraper's
# _PASTE_JS for why typing it key by key breaks the page's own check.
_PASTE_BODY = """
var el = document.querySelector(arguments[0]);
var set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
el.focus(); set.call(el, arguments[1]);
el.dispatchEvent(new Event('input', {bubbles: true}));
el.dispatchEvent(new Event('change', {bubbles: true}));
"""
_CLICK_TEXT_BODY = """
var text = arguments[0];
var els = document.querySelectorAll('button, a, span');
for (var i = 0; i < els.length; i++) {
  if ((els[i].textContent || '').indexOf(text) !== -1) {
    (els[i].closest('button') || els[i]).click(); return true;
  }
}
return false;
"""


class SeleniumSession:
    """lookup_flow's session contract over Selenium + local Chrome."""

    autosolve = False            # no Scraping Browser here — see the docstring

    def __init__(self, args, pool):
        self.args, self.pool = args, pool
        self.autosolve_events = []
        opts = webdriver.ChromeOptions()
        if args.headless:
            opts.add_argument("--headless=new")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument(f"--lang={args.locale}")
        opts.add_argument("--window-size=1400,1000")
        binary = os.environ.get("CHROME_BINARY")
        if binary:
            opts.binary_location = binary
        if pool:
            arg, warning = to_selenium(pool.current)
            if warning:
                logger.warning(warning)
            if arg:
                opts.add_argument(arg)
                logger.info("Using proxy exit %s", mask(pool.current))
        self.driver = webdriver.Chrome(options=opts)
        self.driver.set_page_load_timeout(NAV_TIMEOUT_S)
        self.driver.set_script_timeout(30)
        self.driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument",
                                    {"source": ra.NETWORK_HOOK_JS})
        if args.fingerprint:
            from fingerprint_client import cdp_identity_commands, get_fingerprint
            fp = get_fingerprint(args.twocaptcha_key, tags=args.fp_tags, country=args.fp_country)
            for method, params in cdp_identity_commands(fp):
                self.driver.execute_cdp_cmd(method, params)
            logger.info("Using 2captcha fingerprint %s (%s)", fp.get("id"), fp.get("country"))

    # --- navigation --------------------------------------------------------
    def goto(self, url):
        self.driver.get(url)
        time.sleep(1.5)
        # Selenium exposes no HTTP status; None says so rather than guessing.
        return None, self.html()

    def open_service(self):
        status, _ = self.goto(self.args.url)
        deadline = time.monotonic() + FORM_TIMEOUT_S
        while time.monotonic() < deadline:
            if self.driver.find_elements(By.CSS_SELECTOR, ra.SEL_SEARCH_BUTTON):
                break
            time.sleep(0.4)
        return status, self.html()

    def html(self):
        try:
            return self.driver.page_source
        except WebDriverException:
            return ""

    # --- the form ----------------------------------------------------------
    def net_entries(self):
        try:
            return self.driver.execute_script("return window.__rrNet || [];") or []
        except JavascriptException:
            return []

    def _el(self, selector):
        return self.driver.find_element(By.CSS_SELECTOR, selector)

    def captcha_value(self):
        return self._el(ra.SEL_CAPTCHA_INPUT).get_attribute("value") or ""

    def captcha_image(self):
        return self._el(ra.SEL_CAPTCHA_IMAGE).screenshot_as_png

    def _type(self, selector, text):
        el = self._el(selector)
        el.click()
        # select() then Backspace: el.clear() bypasses React's onChange, and
        # Ctrl+A is "start of line" on macOS.
        self.driver.execute_script("arguments[0].select();", el)
        el.send_keys(Keys.BACKSPACE)
        if text:
            el.send_keys(text)

    def set_captcha(self, text):
        self.driver.execute_script(_PASTE_BODY, ra.SEL_CAPTCHA_INPUT, text)

    def refresh_captcha(self):
        self.driver.execute_script(_CLICK_TEXT_BODY, ra.CAPTCHA_REFRESH_TEXT)

    def set_query(self, text):
        if (self._el(ra.SEL_QUERY).get_attribute("value") or "") != text:
            self._type(ra.SEL_QUERY, text)

    def search_enabled(self):
        return self._el(ra.SEL_SEARCH_BUTTON).is_enabled()

    def click_search(self):
        self._el(ra.SEL_SEARCH_BUTTON).click()

    def fetch_text(self, url):
        res = self.driver.execute_async_script(_FETCH_BODY, url) or {}
        return res.get("s"), res.get("b")

    def runtime_captcha_info(self):
        try:
            return self.driver.execute_script(
                "return (" + captcha_solver.CAPTCHA_DISCOVERY_JS + ")();")
        except JavascriptException as e:
            logger.warning("In-page captcha discovery failed: %s", e)
            return None

    def inject_token(self, token):
        self.driver.execute_script(captcha_solver.INJECT_TOKEN_BODY, token)

    def wait(self, seconds):
        time.sleep(seconds)

    def dump(self, directory, tag):
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, f"{tag}.html"), "w", encoding="utf-8") as f:
            f.write(self.html())
        try:
            self.driver.save_screenshot(os.path.join(directory, f"{tag}.png"))
        except WebDriverException as e:
            logger.debug("screenshot failed: %s", e)

    def close(self):
        try:
            self.driver.quit()
        except Exception as e:  # noqa: BLE001
            logger.debug("teardown: %s", e)


def open_session(args, pool):
    if args.cdp_endpoint:
        raise RuntimeError(
            "The Selenium engine cannot use --cdp-endpoint: the Scraping Browser "
            "authenticates on the WebSocket upgrade and chromedriver has nowhere "
            "to put the password. Use playwright_scraper.py or puppeteer_scraper.py "
            "for the Scraping Browser, or --proxy with this one.")
    return SeleniumSession(args, pool)


def main(argv=None) -> int:
    return cli.main("selenium", __doc__, open_session, argv, supports_cdp=False)


if __name__ == "__main__":
    sys.exit(main())
