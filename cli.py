"""
cli.py
------
The command line every engine exposes, and the mode dispatch behind it.

One parser for all three engines, on purpose: the family has twice found a
dozen flags on its primary engine that its twins did not have while the
README promised "same CLI". Built once, the flag sets cannot drift; the
offline suite still asserts they are identical, in case an engine grows a
flag of its own.

Modes:
  cadastral     (default) full records for --cad-number / --input numbers;
                one captcha per number
  address       the site's free address search: every object at an address,
                as list lines — no captcha. Add --details for the full record
                of each (one captcha each, at most --max-objects)
  dictionaries  the site's code dictionaries (object types = the nine
                "categories", land categories, permitted uses, purposes)
  scan          visit pages of the site and report any captcha on them;
                with the Scraping Browser, let auto-solve try each one

Flags the rest of the family has and this repo does not, each for a reason:
  --pages / --category   there is no listing to page through or categorise;
                         the caller brings the questions
  --concurrency          one Scraping Browser profile is one live connection,
                         and every lookup needs the session's own captcha;
                         run several processes on several profiles instead
  --min-score / --captcha-api   this site's captcha is an image, solved only
                         through the v2 ImageToTextTask
"""

import argparse
import logging
import os
import signal
import sys
import time
from typing import Callable, List, Optional

import env_config
import lookup_flow
import output_writer as ow
import rosreestr_api as ra
from proxy_pool import ProxyError, from_args as pool_from_args, install_log_redaction

logger = logging.getLogger("rosreestr")

MODES = ("cadastral", "address", "dictionaries", "scan")

# Pages of the site the brief names ("registration, the feedback form and
# so on"), taken from the service page's own navigation on 2026-09-29, plus
# the service page itself. --scan-url adds more.
SCAN_URLS = (
    ra.PAGE_URL,
    "https://lk.rosreestr.ru/login",
    "https://rosreestr.gov.ru/eservices/",
    "https://rosreestr.gov.ru/feedback/faq/",
    "https://rosreestr.gov.ru/contacts/reg/",
)


def positive_int(value: str) -> int:
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a whole number") from None
    if n < 1:
        raise argparse.ArgumentTypeError(f"must be 1 or more, got {n}")
    return n


def non_negative_int(value: str) -> int:
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a whole number") from None
    if n < 0:
        raise argparse.ArgumentTypeError(f"must be 0 or more, got {n}")
    return n


def non_negative_float(value: str) -> float:
    try:
        x = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a number") from None
    if x < 0 or x != x:
        raise argparse.ArgumentTypeError(f"must be 0 or more, got {value}")
    return x


def build_parser(engine: str, doc: str) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=f"{engine}_scraper.py", description=doc,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Credentials go in .env (see .env.example), never on the command "
               "line. lk.rosreestr.ru needs a Russian exit.")
    what = p.add_argument_group("what to look up")
    what.add_argument("--mode", choices=MODES, default="cadastral")
    what.add_argument("--cad-number", action="append", metavar="NUMBER",
                      help="a cadastral number, e.g. 77:01:0001044:3030 (repeatable)")
    what.add_argument("--input", metavar="FILE",
                      help="a file of cadastral numbers, one per line (# comments ok)")
    what.add_argument("--address", action="append", metavar="TEXT",
                      help="an address for --mode address (repeatable)")
    what.add_argument("--list-kind", action="append", choices=("OKS", "FLAT", "PARCEL"),
                      help="--mode address: keep only lines of this kind, as the site "
                           "labels them (OKS building/structure, FLAT room, PARCEL land). "
                           "Filtered HERE — the site ignores every objType it is sent — "
                           "so it cannot get past the 100-line cap. Repeatable")
    what.add_argument("--details", action="store_true",
                      help="--mode address: also fetch the full record of each "
                           "object found (one captcha each)")
    what.add_argument("--max-objects", type=positive_int, default=20,
                      help="--mode address --details: full records per address "
                           "(default 20). The list itself is always complete")
    what.add_argument("--scan-url", action="append", metavar="URL",
                      help="--mode scan: an extra page to check (repeatable)")
    what.add_argument("--url", help="the service page (default: ROSREESTR_URL or the "
                                    "real one); override only for a mirror or a test")

    out = p.add_argument_group("output")
    out.add_argument("--format", choices=("json", "csv", "both"), default="both")
    out.add_argument("--out", default="rosreestr_objects",
                     help="output prefix: <out>.json, <out>.csv, <out>.meta.json")
    out.add_argument("--allow-empty", action="store_true",
                     help="write an empty result when every query answered 'not found'")
    out.add_argument("--dump-html", metavar="DIR",
                     help="save the page HTML and a screenshot after each query")

    pace = p.add_argument_group("pacing and retries")
    pace.add_argument("--delay", type=non_negative_float, default=2.0,
                      help="seconds between lookups (default 2)")
    pace.add_argument("--retries", type=non_negative_int, default=1,
                      help="extra rounds per query after a failure (default 1)")
    pace.add_argument("--retry-delay", type=non_negative_float, default=5.0)

    cap = p.add_argument_group("captcha")
    cap.add_argument("--twocaptcha-key", help="2Captcha API key (prefer TWOCAPTCHA_KEY in .env)")
    cap.add_argument("--solve-captcha", choices=("auto", "never"), default="auto",
                     help="'never' refuses to pay for a solve (lookups then need "
                          "the Scraping Browser's auto-solve)")
    cap.add_argument("--captcha-attempts", type=positive_int, default=3,
                     help="paid image solves per query before giving up (default 3)")
    cap.add_argument("--max-solves", type=non_negative_int, default=None,
                     help="hard cap on paid solves for the whole run "
                          "(default: queries x --captcha-attempts)")
    cap.add_argument("--autosolve-wait", type=non_negative_float, default=15.0,
                     help="seconds the Scraping Browser's auto-solve gets before "
                          "2Captcha is paid (default 15; 0 skips it)")
    cap.add_argument("--report-correct", action="store_true",
                     help="also report accepted answers to 2Captcha (wrong ones "
                          "are always reported)")

    net = p.add_argument_group("browser, proxies and identity")
    net.add_argument("--cdp-endpoint",
                     help="Scraping Browser API endpoint (prefer ROSREESTR_CDP_ENDPOINT in .env)")
    net.add_argument("--local", action="store_true",
                     help="launch a local browser (with ROSREESTR_PROXY) even when "
                          "ROSREESTR_CDP_ENDPOINT is set")
    net.add_argument("--proxy", help="one proxy URL (prefer ROSREESTR_PROXY in .env)")
    net.add_argument("--proxy-file", help="a file of proxy URLs, one per line")
    net.add_argument("--proxy-rotate", choices=("per-run", "per-page"), default="per-run",
                     help="per-page = a fresh browser on the next exit for every cadastral "
                          "number (--mode cadastral only)")
    net.add_argument("--proxy-shuffle", action="store_true")
    head = net.add_mutually_exclusive_group()
    head.add_argument("--headless", dest="headless", action="store_true", default=True)
    head.add_argument("--headful", dest="headless", action="store_false")
    net.add_argument("--fingerprint", action="store_true",
                     help="apply a 2Captcha fingerprint to a local browser")
    net.add_argument("--fp-country", default="ru")
    net.add_argument("--fp-tags", default="Windows",
                     help="ONE OS-family tag; the API rejects a list")
    net.add_argument("--locale", default="ru-RU")
    net.add_argument("-v", "--verbose", action="store_true")
    return p


def parse_args(engine: str, doc: str, argv=None, supports_cdp: bool = True):
    parser = build_parser(engine, doc)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    install_log_redaction()
    # Which exits the caller TYPED, before .env fills the rest: a .env that
    # holds both a Scraping Browser endpoint and a proxy is normal (one per
    # kind of run), and must not make every run a usage error.
    typed_cdp = bool(args.cdp_endpoint)
    typed_proxy = bool(args.proxy or args.proxy_file)
    env_config.apply(args)
    args.url = args.url or ra.PAGE_URL
    if args.local:
        if typed_cdp:
            parser.error("--local and --cdp-endpoint contradict each other.")
        args.cdp_endpoint = None
    if not supports_cdp and args.cdp_endpoint:
        # Selenium cannot authenticate to the Scraping Browser. An endpoint
        # the caller TYPED is a usage error; one that came from .env (next to
        # the proxy the README tells people to set) is simply not for this
        # engine, and must not turn every Selenium run into exit 5.
        if typed_cdp:
            parser.error(f"the {engine} engine cannot use --cdp-endpoint: the Scraping "
                         "Browser authenticates on the WebSocket upgrade and "
                         "chromedriver has nowhere to put the password.")
        if not (args.proxy or args.proxy_file):
            parser.error(f"the {engine} engine cannot use ROSREESTR_CDP_ENDPOINT, and no "
                         "proxy is set: lk.rosreestr.ru answers only Russian exits. Set "
                         "ROSREESTR_PROXY (an IP-allowlisted Russian exit — Selenium cannot "
                         "send a proxy password), or use playwright_scraper.py.")
        logger.info("ROSREESTR_CDP_ENDPOINT is set but the %s engine cannot use it; "
                    "launching a local browser on ROSREESTR_PROXY.", engine)
        args.cdp_endpoint = None
    if args.cdp_endpoint and (args.proxy or args.proxy_file):
        if typed_cdp and typed_proxy:
            parser.error("--proxy with --cdp-endpoint: the Scraping Browser already "
                         "proxies (pick its country in the endpoint).")
        if typed_proxy or (args.fingerprint and not typed_cdp):
            logger.info("Using the proxy you gave; ignoring ROSREESTR_CDP_ENDPOINT "
                        "from the environment for this run.")
            args.cdp_endpoint = None
        else:
            logger.info("Using the Scraping Browser (%s); the proxy from the "
                        "environment is for local-browser runs and is ignored here.",
                        "--cdp-endpoint" if typed_cdp else "ROSREESTR_CDP_ENDPOINT")
            args.proxy = args.proxy_file = None
    if args.cdp_endpoint and args.fingerprint:
        parser.error("--fingerprint with --cdp-endpoint: the Scraping Browser brings "
                     "its own identity, and stacking a second one contradicts it.")
    if "," in (args.fp_tags or ""):
        parser.error("--fp-tags takes ONE OS-family tag (e.g. Windows); the "
                     "Fingerprint API answers 400 to a list.")
    if args.solve_captcha == "never":
        args.twocaptcha_key = None
    if args.proxy_rotate == "per-page" and args.mode != "cadastral":
        # Implemented for the one route that has a query per browser worth
        # rotating; refused elsewhere rather than silently ignored (audit #7).
        parser.error("--proxy-rotate per-page is implemented for --mode cadastral only.")
    if args.mode == "cadastral":
        if not (args.cad_number or args.input):
            parser.error("--mode cadastral needs --cad-number or --input")
        if args.input and not os.path.isfile(args.input):
            parser.error(f"--input {args.input!r} is not a file")
        args.queries = lookup_flow.read_queries(args)
        bad = [q for q in args.queries if not ra.is_cad_number(q)]
        if bad:
            parser.error(f"not cadastral numbers: {', '.join(repr(b) for b in bad[:5])}"
                         + (" ..." if len(bad) > 5 else "")
                         + " — an address goes to --mode address --address")
    elif args.mode == "address":
        if not args.address:
            parser.error("--mode address needs --address")
    if args.max_solves is None:
        n = len(getattr(args, "queries", []) or []) or (
            len(args.address or []) * args.max_objects if args.details else 1)
        args.max_solves = max(1, n) * args.captcha_attempts
    return args


def query_spec(args) -> dict:
    """What a run ASKED, beyond its input lines — recorded in the sidecar and
    compared by diff_runs.py. Two runs are one question only if these agree."""
    details = bool(getattr(args, "details", False))
    return {"spec_version": 1,
            "url": args.url,
            "mode": args.mode,
            "list_kind": sorted(getattr(args, "list_kind", None) or []) or None,
            "details": details,
            "max_objects": args.max_objects if details else None}


class _Interrupt:
    """Ctrl-C finishes the current query and writes what was gathered."""

    def __init__(self):
        self.hit = False
        try:
            signal.signal(signal.SIGINT, self._on)
        except ValueError:   # not the main thread (the offline suite)
            pass

    def _on(self, *_):
        if self.hit:
            raise KeyboardInterrupt
        self.hit = True
        logger.warning("Interrupted — finishing the current query, then writing "
                       "what was gathered. Ctrl-C again to abort.")


def run(args, open_session: Callable, engine: str) -> int:
    """Dispatch a parsed command line to its mode. Returns the exit code.

    `open_session(args, pool)` returns an engine session (see lookup_flow).
    """
    try:
        pool = pool_from_args(args)
    except ProxyError as e:
        logger.error("%s", e)
        return ow.EXIT_USAGE
    solver = lookup_flow.Solver(api_key=args.twocaptcha_key)
    budget = lookup_flow.Budget(limit=args.max_solves)
    interrupt = _Interrupt()
    started = time.monotonic()

    holder = [None]
    try:
        session = open_session(args, pool)
    except Exception as e:  # noqa: BLE001 — a launch or a CDP connect that failed
        from proxy_pool import redact_secret_patterns
        logger.error("Could not start the browser: %s", redact_secret_patterns(str(e))[:400])
        return _finish_failed(args, engine, "browser_start_failed")
    holder[0] = session
    try:
        if args.mode == "scan":
            return _run_scan(args, session, solver, budget)
        stop = lookup_flow.open_or_explain(session, _exit_country(args, pool))
        if stop:
            if stop == "service_page_not_served" and hasattr(session, "dump"):
                # Always, not only with --dump-html: "the form never appeared"
                # is undiagnosable without the page that appeared instead.
                import os
                where = os.path.dirname(os.path.abspath(args.out))
                session.dump(where, os.path.basename(args.out) + ".debug")
                logger.error("Saved what the page showed instead: %s.debug.html / .png",
                             args.out)
            return _finish_failed(args, engine, stop)
        if args.mode == "dictionaries":
            data = lookup_flow.fetch_dictionaries(session)
            if not data:
                return ow.EXIT_FETCH_FAILED
            path = f"{args.out}.json"
            lookup_flow.dump_json(path, data)
            missing = [n for n in ra.DICTIONARIES if n not in data]
            print(f"[+] Saved {len(data)} of {len(ra.DICTIONARIES)} dictionaries -> {path}")
            if missing:
                logger.warning("Not fetched: %s — a partial set (exit %d).",
                               ", ".join(missing), ow.EXIT_PARTIAL)
                return ow.EXIT_PARTIAL
            return ow.EXIT_OK
        if args.mode == "address":
            return _run_address(args, session, solver, budget, engine, interrupt)
        outcomes, stop_reason = _run_cadastral_rotating(
            args, holder, pool, solver, budget, args.queries, 1, interrupt,
            open_session)
        return ow.finish_run(outcomes, args.out, args.format, args.allow_empty,
                             mode=args.mode, engine=engine,
                             queries_requested=len(args.queries),
                             stop_reason=stop_reason, spec=query_spec(args))
    finally:
        logger.info("Paid captcha solves this run: %d (cap %d). %.0fs.",
                    budget.spent, budget.limit, time.monotonic() - started)
        # Whichever session is live now; a rotation already closed the rest.
        if holder[0] is not None:
            try:
                holder[0].close()
            except Exception:  # noqa: BLE001 — teardown must not mask the result
                pass


def _run_cadastral_rotating(args, holder, pool, solver, budget, queries,
                            start_index, interrupt, open_session):
    """Run numbers; with --proxy-rotate per-page, one fresh browser each.

    `holder` is a one-element list holding the live session, so the caller
    closes whichever session is current when this returns — not the first
    one, which a rotation has already closed.
    """
    if not (pool and pool.rotates_per_page() and len(pool) > 1):
        return lookup_flow.run_cadastral(holder[0], args, solver, budget, queries,
                                         start_index, lambda: interrupt.hit)
    outcomes = []
    for offset, q in enumerate(queries):
        if interrupt.hit:
            return outcomes, "interrupted"
        if offset:
            if args.delay > 0:
                # run_cadastral gets one number at a time here, so its own
                # between-numbers delay never fires; apply --delay here.
                if holder[0] is not None:
                    holder[0].wait(args.delay)
                else:
                    time.sleep(args.delay)
            pool.advance("per-page rotation")
            try:
                holder[0].close()
            except Exception:  # noqa: BLE001 — teardown must not mask the result
                pass
            holder[0] = None
            try:
                holder[0] = open_session(args, pool)
            except Exception as e:  # noqa: BLE001 — this exit failed; the next may not
                from proxy_pool import redact_secret_patterns
                logger.error("Could not start the browser on the next exit: %s",
                             redact_secret_patterns(str(e))[:300])
                outcomes.append(ow.QueryOutcome(index=start_index + offset, query=q,
                                                reason="browser_start_failed"))
                continue
            if lookup_flow.open_or_explain(holder[0], _exit_country(args, pool)):
                outcomes.append(ow.QueryOutcome(index=start_index + offset, query=q,
                                                reason="page_load_failed"))
                continue
        if holder[0] is None:
            outcomes.append(ow.QueryOutcome(index=start_index + offset, query=q,
                                            reason="browser_start_failed"))
            continue
        got, stop = lookup_flow.run_cadastral(holder[0], args, solver, budget, [q],
                                              start_index + offset)
        outcomes.extend(got)
        if stop != "completed":
            return outcomes, stop
    return outcomes, "completed"


def _run_address(args, session, solver, budget, engine, interrupt) -> int:
    outcomes: List[ow.QueryOutcome] = []
    total, index, planned = 0, 1, 0
    stop_reason = "completed"
    visited = 0
    for address in args.address:
        if interrupt.hit:
            stop_reason = "interrupted"
            break
        if visited and args.delay > 0:
            # --delay between addresses too, not only between numbers (audit #7).
            session.wait(args.delay)
        visited += 1
        o, n = lookup_flow.run_address(session, args, index, address)
        outcomes.append(o)
        index += 1
        total += n or 0
        logger.info("address %r: %s", address,
                    f"{n} object(s) listed" if o.answered else f"FAILED ({o.reason})")
        if args.details and o.answered and o.records:
            numbers = [r.sku for r in o.records][:args.max_objects]
            # A number already fetched in full earlier in THIS run is not paid
            # for again; the merge puts that record in this address's place.
            bought = {r.sku for x in outcomes for r in x.records if r.detail_level == "full"}
            if bought & set(numbers):
                logger.info("%d object(s) at %r were already fetched in full this run; "
                            "not paying for them again.", len(bought & set(numbers)), address)
            numbers = [n for n in numbers if n not in bought]
            planned += len(numbers)
            if len(o.records) > args.max_objects:
                logger.info("Fetching full records for the first %d of %d "
                            "(--max-objects).", args.max_objects, len(o.records))
            got, stop = lookup_flow.run_cadastral(session, args, solver, budget,
                                                  numbers, index, lambda: interrupt.hit)
            for g in got:
                g.derived = True
            outcomes.extend(got)
            index += len(numbers)
            _put_full_records_in_place(o, got)
            if stop != "completed":
                # Carried through, never replaced by "completed": the rest
                # of this address's objects and every later address were not
                # looked at, and the sidecar has to say so.
                stop_reason = stop
                break
    # What was asked: every address given, plus every full record planned
    # for the addresses reached. Addresses never reached count once each.
    requested = len(args.address) + planned
    return ow.finish_run(outcomes, args.out, args.format, args.allow_empty,
                         mode="address" + ("+details" if args.details else ""),
                         engine=engine, queries_requested=requested,
                         stop_reason=stop_reason, address_results=total,
                         spec=query_spec(args))


def _put_full_records_in_place(address_outcome, detail_outcomes) -> None:
    """Swap each list line for its full record, IN the list's position.

    The detail outcomes keep `found` (so none reads as "not found") but give
    their rows to the address answer; dedupe then sees each object once, in
    the order the site listed them.
    """
    full = {}
    for d in detail_outcomes:
        for r in d.records:
            full[r.sku] = r
        d.records = []
    merged = []
    for line in address_outcome.records:
        rec = full.pop(line.sku, None)
        if rec is None:
            merged.append(line)
            continue
        # The row now sits where the list put it, so it says so: the
        # address query's page and position, and the list's own kind label.
        rec.page, rec.position, rec.list_kind = line.page, line.position, line.list_kind
        merged.append(rec)
    address_outcome.records = merged


def _run_scan(args, session, solver, budget) -> int:
    urls = list(SCAN_URLS) + list(args.scan_url or [])
    results = []
    for url in urls:
        r = lookup_flow.scan_page(session, args, solver, budget, url)
        results.append(r.__dict__)
        if r.error:
            logger.warning("scan %s -> did not load: %s", url, r.error.splitlines()[0][:200])
            continue
        found = r.widget or ("image captcha" if r.image_captcha else None)
        logger.info("scan %s -> HTTP %s (%s), %s%s", url, r.status, r.title,
                    found or "no captcha",
                    f", auto-solve: {r.autosolve_events}" if r.autosolve_events else "")
    path = f"{args.out}.scan.json"
    lookup_flow.dump_json(path, results)
    print(f"[+] Saved scan of {len(results)} page(s) -> {path}")
    # Loaded = no error. Not the HTTP status: Selenium never has one, and
    # every scan through it would have "failed".
    return ow.EXIT_OK if any(not r["error"] for r in results) else ow.EXIT_FETCH_FAILED


def _exit_country(args, pool) -> Optional[str]:
    """The country this run's exit asks for — the advice on a timeout depends on it."""
    return lookup_flow.exit_country(args.cdp_endpoint, pool.current if pool else None)


def _finish_failed(args, engine, stop) -> int:
    n = len(getattr(args, "queries", []) or args.address or [])
    outcomes = []
    return ow.finish_run(outcomes, args.out, args.format, args.allow_empty,
                         mode=args.mode, engine=engine, queries_requested=max(n, 1),
                         stop_reason=stop, spec=query_spec(args))


def main(engine: str, doc: str, open_session: Callable, argv=None,
         supports_cdp: bool = True) -> int:
    args = parse_args(engine, doc, argv, supports_cdp=supports_cdp)
    try:
        return run(args, open_session, engine)
    except KeyboardInterrupt:
        logger.error("Aborted.")
        return ow.EXIT_CRASH


if __name__ == "__main__":
    sys.exit("Run one of the engines: playwright_scraper.py, puppeteer_scraper.py, "
             "selenium_scraper.py")
