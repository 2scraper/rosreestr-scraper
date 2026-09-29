"""
lookup_flow.py
--------------
The one loop every engine runs: open the service, get a captcha answered,
ask, read the answer, decide what the answer means. Shared so that three
engines cannot disagree about whether a query is worth another paid solve,
whether a 406 is a wrong captcha or a block, or what exit code a run earns —
the drift this family has paid for every time the triage lived in three
copies.

Engines hand in a SESSION: a small object whose methods are driver
primitives (type into this field, click that button, return the image's
bytes, return what the network hook recorded). No JavaScript crosses this
boundary — Selenium's execute_script takes a function BODY and Playwright
takes an arrow function, so each engine spells its own evaluate calls and
this module names the operation instead.

The session contract (all engines implement exactly this):

    open_service() -> (status|None, html)   navigate, wait for the form
    goto(url) -> (status|None, html)        any page, for --mode scan
    html() -> str
    net_entries() -> list[dict]              window.__rrNet (see rosreestr_api)
    captcha_value() -> str
    captcha_image() -> bytes                 the <img> as the page shows it
    set_captcha(text)                        type into #captcha ("" clears it)
    refresh_captcha()                        click "Обновить картинку"
    set_query(text)                          type into #query
    search_enabled() -> bool
    click_search()
    fetch_text(url) -> (status|None, body)   same-origin GET from the page
    wait(seconds)
    autosolve                                True when Captcha.setAutoSolve
                                             was accepted (Scraping Browser)
    autosolve_events                         list the CDP handlers append to
    runtime_captcha_info() -> dict|None      CAPTCHA_DISCOVERY_JS result
    inject_token(token)
    dump(directory, tag)                     HTML + screenshot, for --dump-html
    close()

How a captcha gets answered, in order (the brief's "the Scraping Browser's
auto-solve must try first — do not forget Captcha.setAutoSolve"):

  1. auto-solve, when the session is on the Scraping Browser and it was
     accepted: wait up to --autosolve-wait for the field to be filled AND the
     site's own check to answer 200. Measured 2026-09-29: enabled, did not
     fill this image captcha within 20 s — so this step usually ends in a
     timeout, and it is still first, because it is free when it works.
  2. 2Captcha ImageToTextTask on the image as the page shows it.
  3. the site says wrong (the check answers 403, or the lookup 406) ->
     reportIncorrect, fresh image, again — up to --captcha-attempts, and
     never past --max-solves for the run.
"""

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

import captcha_solver
import rosreestr_api as ra
from output_writer import QueryOutcome, Record

logger = logging.getLogger("lookup_flow")

# Every deadline in this module reads this clock. The offline suite swaps it
# for one that advances when a fake session "waits", so a timeout path is
# exercised in no time at all instead of spinning until a real deadline.
_clock = time.monotonic

# How long the site's own captcha check may take to answer after the field
# is filled. It is one small GET; 6 s is generous.
CAPTCHA_CHECK_TIMEOUT = 6.0
# How long a lookup may take to answer after the button is clicked.
SEARCH_TIMEOUT = 25.0
# How long to wait for the page to fetch its NEXT image after a lookup (it
# does so by itself, measured on every search on 2026-09-29).
NEW_IMAGE_TIMEOUT = 6.0
# How long --mode scan keeps looking at a page before saying "no captcha".
SCAN_SETTLE = 10.0


@dataclass
class Budget:
    """The run's paid-solve budget, and the only place it is spent.

    One helper, called from every place a solve is bought — the family has
    shipped a per-page cap that read like a limit while two of the three call
    sites spent outside it ("A cap that nothing enforces is a bill").
    """
    limit: int
    spent: int = 0

    def take(self) -> bool:
        if self.spent >= self.limit:
            return False
        self.spent += 1
        return True


@dataclass
class Solver:
    """The paid solver, injectable so the offline suite can drive the flow."""
    api_key: Optional[str]
    solve_image: Callable = captcha_solver.solve_image
    report: Callable = captcha_solver.report_image


@dataclass
class CaptchaState:
    """Whether the field currently holds an answer the site accepted."""
    ready: bool = False
    task_id: Optional[int] = None      # the paid task behind the answer, if any
    autosolved: bool = False


def _new_entries(session, since: int) -> List[dict]:
    return [e for e in session.net_entries() if int(e.get("n") or 0) > since]


def _last_n(session) -> int:
    entries = session.net_entries()
    return max((int(e.get("n") or 0) for e in entries), default=0)


def _wait_for(session, since: int, kind: str, timeout: float) -> Optional[dict]:
    """The newest recorded entry of `kind` after `since`, or None on timeout."""
    deadline = _clock() + timeout
    while True:
        hits = [e for e in _new_entries(session, since) if ra.net_kind(e) == kind]
        if hits:
            return hits[-1]
        if _clock() >= deadline:
            return None
        session.wait(0.25)


def _await_image_settle(session, since: int, quiet: float = 0.8) -> None:
    """Wait for the first new captcha image after `since`, then until no
    further one arrives for `quiet` seconds.

    After a lookup the page loads its next image by itself, and a refresh
    loads another; the first to land is not necessarily the one that stays.
    Reading the image before the swaps are over could screenshot one about
    to be replaced (found by an audit, 2026-09-29).
    """
    hit = _wait_for(session, since, "captcha_image", NEW_IMAGE_TIMEOUT)
    while hit is not None:
        hit = _wait_for(session, int(hit.get("n") or 0), "captcha_image", quiet)


def _check_text(entry: dict) -> str:
    from urllib.parse import unquote
    return unquote(str(entry.get("u") or "").split("?")[0].rsplit("/", 1)[-1])


def _check_status(session, since: int, text: Optional[str] = None) -> Optional[int]:
    """The site's verdict on `text`: the status of ITS check, not any check.

    An engine that types like a person (pyppeteer, Selenium) makes the page
    check every prefix — /captcha/д, /captcha/ду, … — and the first verdict
    to arrive is the one on a half-typed answer. Only the check whose path is
    exactly the full text counts. With no `text` (auto-solve typed it), the
    newest check does.
    """
    deadline = _clock() + CAPTCHA_CHECK_TIMEOUT
    while True:
        checks = [e for e in _new_entries(session, since) if ra.net_kind(e) == "captcha_check"]
        if text is not None:
            checks = [e for e in checks if _check_text(e) == text]
        if checks and checks[-1].get("s") is not None:
            return int(checks[-1]["s"])
        if _clock() >= deadline:
            return None
        session.wait(0.25)


def _await_fresh_image(session) -> None:
    """After a lookup the page fetches its next image by itself. Wait for it,
    so the image read next is the live one and not the one just spent.

    Measured from the CLICK, not from the lookup's answer: the new image can
    finish loading before the answer does, so "an image after the answer"
    missed it on every query of the first live run and asked for a second
    one each time.
    """
    clicked = getattr(session, "_rr_clicked_at", None)
    if clicked is None:
        return
    if _wait_for(session, clicked, "captcha_image", NEW_IMAGE_TIMEOUT) is None:
        logger.info("The page did not load a new captcha after the last lookup; "
                    "asking for one.")
        since = _last_n(session)
        session.refresh_captcha()
        _wait_for(session, since, "captcha_image", NEW_IMAGE_TIMEOUT)


def _try_autosolve(session, args, outcome: QueryOutcome) -> CaptchaState:
    """Give the Scraping Browser's auto-solve its turn. Free when it works."""
    if not getattr(session, "autosolve", False) or args.autosolve_wait <= 0:
        return CaptchaState()
    since = _last_n(session)
    # Every query gives auto-solve its turn, as the brief asks. But a turn
    # costs wall time, and on this site's image captcha it has answered
    # nothing: 0 of 3 queries on 2026-09-29, with not one Captcha.* event.
    # Once it has stayed SILENT (no event at all) through two full waits,
    # later queries give it a short turn instead of the full one; any event
    # from it restores the full wait.
    silent = getattr(session, "_rr_autosolve_silent", 0)
    wait = args.autosolve_wait if silent < 2 else min(args.autosolve_wait, 3.0)
    events_before = len(getattr(session, "autosolve_events", []))
    deadline = _clock() + wait
    while _clock() < deadline:
        value = session.captcha_value()
        if value:
            status = _check_status(session, since, value)
            if status == 200:
                logger.info("[Scraping Browser] auto-solve answered the captcha.")
                outcome.autosolved = True
                return CaptchaState(ready=True, autosolved=True)
            logger.info("[Scraping Browser] auto-solve typed an answer the site "
                        "refused (%s).", status)
            break
        session.wait(0.5)
    heard = len(getattr(session, "autosolve_events", [])) > events_before
    session._rr_autosolve_silent = 0 if heard else silent + 1
    logger.info("Auto-solve did not answer within %ss%s; using 2Captcha.", wait,
                "" if heard else " and sent no Captcha.* event")
    return CaptchaState()


def _solve_paid(session, args, solver: Solver, budget: Budget,
                outcome: QueryOutcome, *, refresh: bool) -> Tuple[CaptchaState, Optional[str]]:
    """One paid attempt on the image currently shown. Returns (state, reason)."""
    if not solver.api_key:
        return CaptchaState(), "captcha_no_key"
    if budget.spent >= budget.limit:
        return CaptchaState(), "captcha_budget_exhausted"
    if refresh:
        since = _last_n(session)
        session.refresh_captcha()
        _await_image_settle(session, since)
    session.set_captcha("")
    image = session.captcha_image()
    # Taken HERE, once the image is in hand and the next call is the paid
    # one — so the counter is the bill, not the number of attempts made.
    if not budget.take():
        return CaptchaState(), "captcha_budget_exhausted"
    outcome.captcha_solves += 1
    try:
        sol = solver.solve_image(solver.api_key, image)
    except (RuntimeError, TimeoutError) as e:
        logger.warning("2Captcha could not read the image: %s", e)
        return CaptchaState(), "captcha_solver_error"
    since = _last_n(session)
    session.set_captcha(sol.text)
    status = _check_status(session, since, sol.text)
    if status == 200:
        return CaptchaState(ready=True, task_id=sol.task_id), None
    outcome.captcha_rejected += 1
    logger.info("The site rejected the answer %r (captcha check answered %s).",
                sol.text, status)
    solver.report(solver.api_key, sol.task_id, False)
    return CaptchaState(), "captcha_rejected" if status == 403 else "captcha_check_no_answer"


def _get_captcha(session, args, solver: Solver, budget: Budget,
                 outcome: QueryOutcome, first: bool) -> Tuple[CaptchaState, Optional[str]]:
    """Auto-solve first (on the query's first round), then paid attempts.

    Paid attempts are counted PER QUERY, across rounds: --captcha-attempts is
    what one number may cost before it is given up on. An earlier version
    reset it every --retries round, so a number the site kept refusing
    bought attempts x (retries + 1) solves and ate the next number's budget
    (found by an independent audit, 2026-09-29).
    """
    if first:
        state = _try_autosolve(session, args, outcome)
        if state.ready:
            return state, None
    reason, made = None, 0
    while outcome.captcha_solves < args.captcha_attempts:
        # A fresh image for every paid attempt after this call's first; the
        # round itself already started from a fresh one.
        state, reason = _solve_paid(session, args, solver, budget, outcome,
                                    refresh=made > 0)
        made += 1
        if state.ready:
            return state, None
        # A solver error (no slot, unsolvable) is transient: the next attempt,
        # on a fresh image, may well succeed. Only a missing key or an empty
        # budget ends the attempts.
        if reason in ("captcha_no_key", "captcha_budget_exhausted"):
            break
    return CaptchaState(), reason or "captcha_attempts_exhausted"


def _click(session) -> bool:
    """Press the search button; a driver error here is a failed round, not a crash."""
    try:
        session.click_search()
        return True
    except Exception as e:  # noqa: BLE001 — each driver raises its own type
        from proxy_pool import redact_secret_patterns
        logger.warning("Pressing search failed: %s",
                       redact_secret_patterns(str(e)).splitlines()[0][:200])
        return False


def lookup_one(session, args, solver: Solver, budget: Budget, index: int,
               query: str) -> QueryOutcome:
    """Look up one cadastral number. Never raises for a site-shaped failure.

    Rounds (--retries + 1) repeat what is transient — no answer to the
    search, a server error, an answer the lookup refused after the check had
    accepted it. A captcha that could not be answered within
    --captcha-attempts ends the query: more rounds would only buy more of
    the same refusals.
    """
    outcome = QueryOutcome(index=index, query=query)
    number = ra.normalise_cad_number(query)
    _await_fresh_image(session)
    for round_ in range(args.retries + 1):
        # Every round starts clean: a refusal in an earlier round says
        # nothing about why this one failed.
        outcome.blocked = False
        if round_:
            session.wait(args.retry_delay)
            # The previous round's image was spent (or its answer refused):
            # a round starts from a fresh one, never a stale screenshot.
            since = _last_n(session)
            session.refresh_captcha()
            _await_image_settle(session, since)
        # The field still holds the PREVIOUS query's answer after a search
        # (measured: the value stays, the search button stays enabled) and
        # that answer is spent. Clearing it first is what stops the
        # auto-solve wait from mistaking it for an answer of its own.
        session.set_captcha("")
        session.set_query(number)
        state, reason = _get_captcha(session, args, solver, budget, outcome,
                                     first=round_ == 0)
        if not state.ready:
            outcome.reason = reason or "captcha_not_answered"
            # A captcha the site keeps refusing is a gate, not a transient.
            outcome.blocked = reason in ("captcha_rejected", "captcha_no_key",
                                         "captcha_budget_exhausted",
                                         "captcha_attempts_exhausted")
            return outcome
        # The button follows the check's verdict asynchronously; give it a
        # moment rather than clicking a disabled button.
        for _ in range(12):
            if session.search_enabled():
                break
            session.wait(0.25)
        else:
            session.set_query(number)
            session.wait(0.5)
        since = _last_n(session)
        session._rr_clicked_at = since
        if not _click(session):
            outcome.reason = "lookup_click_failed"
            continue
        hit = _wait_for(session, since, "on", SEARCH_TIMEOUT)
        if hit is None:
            outcome.reason = "lookup_no_answer"
            continue
        kind, data = ra.classify_on(hit.get("s"), hit.get("body"))
        if kind == ra.ANSWERED:
            outcome.answered, outcome.reason, outcome.blocked = True, None, False
            outcome.records = ra.parse_on(data, query=query, index=index)
            outcome.found = len(outcome.records)
            if state.task_id is not None and args.report_correct:
                solver.report(solver.api_key, state.task_id, True)
            return outcome
        if kind == ra.WRONG_CAPTCHA:
            # The check said yes and the lookup said no: the answer was
            # spent or refused after all. Only a PAID answer can be reported.
            outcome.captcha_rejected += 1
            if state.task_id is not None:
                solver.report(solver.api_key, state.task_id, False)
            outcome.reason = "captcha_rejected_by_lookup"
            continue
        outcome.reason = f"lookup_{kind}_{hit.get('s')}"
        outcome.blocked = kind == ra.REFUSED
    return outcome


def _between(session, args):
    if args.delay > 0:
        session.wait(args.delay)


def run_cadastral(session, args, solver: Solver, budget: Budget,
                  queries: List[str], start_index: int = 1,
                  deadline_reached: Callable[[], bool] = lambda: False
                  ) -> Tuple[List[QueryOutcome], str]:
    """Look up every number in order. Returns (outcomes, stop_reason)."""
    outcomes: List[QueryOutcome] = []
    for offset, q in enumerate(queries):
        if deadline_reached():
            return outcomes, "interrupted"
        try:
            o = lookup_one(session, args, solver, budget, start_index + offset, q)
        except Exception as e:  # noqa: BLE001 — a driver error on ONE query
            # A selector that stopped matching or a page that died mid-query
            # fails that query, loudly, and the run goes on; a traceback and
            # exit 1 would throw away every answer already bought.
            from proxy_pool import redact_secret_patterns
            logger.error("query %d raised: %s", start_index + offset,
                         redact_secret_patterns(str(e)).splitlines()[0][:300])
            logger.debug("traceback", exc_info=True)
            o = QueryOutcome(index=start_index + offset, query=q,
                             reason="page_fetch_raised")
        outcomes.append(o)
        if args.dump_html:
            # On success too: a right count with a column silently empty is
            # only diagnosable from the exact bytes.
            session.dump(args.dump_html, f"query{start_index + offset}")
            # The page's own traffic for this query — the raw answer the row
            # was parsed from, and the source of any new fixture.
            import os
            dump_json(os.path.join(args.dump_html, f"query{start_index + offset}.net.json"),
                      session.net_entries())
        logger.info("query %d/%d %s: %s", start_index + offset,
                    start_index + len(queries) - 1, q,
                    f"{len(o.records)} record(s)" if o.answered else f"FAILED ({o.reason})")
        if o.reason in ("captcha_no_key", "captcha_budget_exhausted"):
            return outcomes, o.reason
        if offset + 1 < len(queries):
            _between(session, args)
    return outcomes, "completed"


def same_origin(url: str) -> str:
    """The path+query of `url`, for a fetch made FROM the page.

    The page fetches relative to its own origin, as the site's front end
    does; an absolute https://lk.rosreestr.ru/... from a page served
    anywhere else (a --url mirror, the offline mock) is a cross-origin
    request the browser refuses.
    """
    from urllib.parse import urlsplit
    parts = urlsplit(url)
    return parts.path + (f"?{parts.query}" if parts.query else "")


def _fetch(session, url):
    """session.fetch_text on the page's own origin; a raise is a failed fetch."""
    try:
        return session.fetch_text(same_origin(url))
    except Exception as e:  # noqa: BLE001 — a driver error fails this fetch only
        from proxy_pool import redact_secret_patterns
        logger.warning("fetch %s raised: %s", same_origin(url)[:120],
                       redact_secret_patterns(str(e)).splitlines()[0][:200])
        return None, None


def run_address(session, args, index: int, address: str) -> Tuple[QueryOutcome, Optional[int]]:
    """The free address search: one GET, no captcha. (outcome, n_results)."""
    outcome = QueryOutcome(index=index, query=address)
    url = ra.address_search_url(address)
    for round_ in range(args.retries + 1):
        if round_:
            session.wait(args.retry_delay)
        status, body = _fetch(session, url)
        if status == 200 and body is not None:
            try:
                outcome.records = ra.parse_address_search(body, query=address, index=index)
            except ValueError:
                outcome.reason = "address_search_unreadable"
                continue
            outcome.answered, outcome.reason = True, None
            served = len(outcome.records)
            outcome.found = served
            if served >= ra.ADDRESS_CAP:
                outcome.capped = True
                logger.warning("The address search returned its maximum of %d lines "
                               "for %r: more objects exist, and which %d come back "
                               "changes between calls. Narrow the address itself "
                               "(house, building, room number) — the site offers no "
                               "other filter.", ra.ADDRESS_CAP, address, ra.ADDRESS_CAP)
            if getattr(args, "list_kind", None):
                # Positions stay the site's own; the kept lines are a subset
                # of what the site chose to serve, before this filter.
                outcome.records = [r for r in outcome.records if r.list_kind in args.list_kind]
                # "Found" is what answers the question asked — objects of this
                # kind — so an address with none of them reads as not found
                # (and its served count stays in address_results).
                outcome.found = len(outcome.records)
                logger.info("Kept %d of %d lines of kind %s.", len(outcome.records), served,
                            "/".join(args.list_kind))
            return outcome, served
        outcome.reason = f"address_search_{status}"
        outcome.blocked = status in ra.REFUSAL_STATUSES
    return outcome, None


def open_or_explain(session) -> Optional[str]:
    """Open the service. Returns None, or the stop_reason if it never came up."""
    try:
        status, html = session.open_service()
    except Exception as e:  # noqa: BLE001 — engines raise their driver's types
        from proxy_pool import redact_secret_patterns
        text = redact_secret_patterns(str(e))
        logger.error("The service page did not load: %s", text[:400])
        logger.error("lk.rosreestr.ru does not answer from outside Russia (every "
                     "host timed out at the TCP connect from a Belgian exit, "
                     "2026-09-29). Use a Russian exit: --proxy with a -region-ru "
                     "login, or --cdp-endpoint with country-ru.")
        return "proxy_failed" if "ERR_PROXY" in text or "ERR_TUNNEL" in text else "page_load_failed"
    if not getattr(session, "needs_form", True):
        # A one-shot transport (the Scraper API) has no form to find; the
        # site answering at all is what open_service() proves.
        if status == 200:
            return None
        logger.error("The site did not answer through this transport (HTTP %s).", status)
        return "service_unreachable"
    if not ra.is_service_page(html):
        logger.error("Loaded %s but it is not the search form (HTTP %s, title %r).",
                     ra.PAGE_URL, status, ra.page_title(html))
        return "service_page_not_served"
    return None


def read_queries(args) -> List[str]:
    """The run's questions, in the order given: --cad-number, then --input."""
    out = list(args.cad_number or [])
    if args.input:
        with open(args.input, encoding="utf-8-sig") as f:
            for line in f:
                line = line.split("#", 1)[0].strip().strip(",;")
                if line:
                    out.append(line)
    return out


def fetch_dictionaries(session) -> dict:
    """{NAME: {code: label}} for every dictionary the page loads."""
    out = {}
    for name in ra.DICTIONARIES:
        status, body = _fetch(session, ra.dictionary_url(name))
        if status == 200 and body:
            out[name] = ra.parse_dictionary(body)
        else:
            logger.warning("dictionary %s answered %s", name, status)
    return out


@dataclass
class ScanResult:
    url: str
    status: Optional[int] = None
    title: Optional[str] = None
    image_captcha: bool = False
    widget: Optional[str] = None          # "turnstile" / "recaptcha_v2" / ...
    sitekey: Optional[str] = None
    autosolve_events: List[str] = field(default_factory=list)
    solved: Optional[bool] = None
    error: Optional[str] = None


def scan_page(session, args, solver: Solver, budget: Budget, url: str) -> ScanResult:
    """Look for a captcha on one page of the site, and try to clear it.

    The brief: "if there is a captcha on ANY page — registration, the
    feedback form and so on — the Scraping Browser's auto-solve must try to
    solve it". With setAutoSolve enabled the page is left to auto-solve for
    --autosolve-wait; a widget it did not clear is then solved through
    2Captcha and the token injected, if --solve-captcha allows it.
    """
    res = ScanResult(url=url)
    events_before = len(getattr(session, "autosolve_events", []))
    try:
        res.status, html = session.goto(url)
    except Exception as e:  # noqa: BLE001
        from proxy_pool import redact_secret_patterns
        res.error = redact_secret_patterns(str(e))[:300]
        return res
    # A single-page app paints its form (and its captcha) after load: the
    # first scan of the service page read it 1.5 s in and reported "no
    # captcha" on a page that always has one. So look again until something
    # shows or SCAN_SETTLE passes — a false "none here" is the one answer
    # this mode must not give.
    deadline = _clock() + SCAN_SETTLE
    while True:
        res.title = ra.page_title(html)
        res.image_captcha = ra.has_image_captcha(html)
        static = captcha_solver.detect_in_html(html, url)
        runtime = captcha_solver.challenge_from_discovery(session.runtime_captcha_info(), url)
        challenge = captcha_solver.reconcile_detections(static, runtime)
        if res.image_captcha or challenge or _clock() >= deadline:
            break
        session.wait(1.0)
        html = session.html()
    if challenge:
        res.widget, res.sitekey = challenge.kind, challenge.sitekey
    if (challenge or res.image_captcha) and getattr(session, "autosolve", False):
        session.wait(args.autosolve_wait)
    res.autosolve_events = [str(e) for e in session.autosolve_events[events_before:]]
    if any("solveFinished" in e for e in res.autosolve_events):
        res.solved = True
    elif challenge and challenge.sitekey and args.solve_captcha != "never":
        if solver.api_key and budget.take():
            try:
                token = captcha_solver.solve(challenge, solver.api_key)
                session.inject_token(token)
                res.solved = True
            except (RuntimeError, TimeoutError) as e:
                logger.warning("2Captcha could not solve the %s on %s: %s",
                               challenge.kind, url, e)
                res.solved = False
    return res


def dump_json(path: str, data) -> None:
    from output_writer import _atomic_write
    _atomic_write(path, lambda f: json.dump(data, f, ensure_ascii=False, indent=2))
