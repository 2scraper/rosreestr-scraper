#!/usr/bin/env python3
"""
smoke_test.py
--------------
The offline suite: one file of plain functions, no pytest, no conftest.
`tests/test_smoke.py` wraps it as a single pytest test.

    python3 smoke_test.py          # everything that needs no browser
    python3 smoke_test.py --e2e    # also drive every INSTALLED engine, with a
                                   # real local browser, against tests/mock_site.py

It passes with NO engine library installed: each engine import is guarded
and the skip is printed. CI's engine jobs install one engine each, run with
--e2e, and fail if that engine's group skipped — "skipped, engine absent"
reads identically to a broken import.

HERMETIC. The suite clears every variable the code reads and points the .env
loader at /dev/null before anything else runs, so no check can read a
developer's credentials or reach the network. No check sleeps for real.

The fixtures are real lk.rosreestr.ru answers (fixtures.json, cut by
make_fixtures.py from a live probe on 2026-09-29) — not verbatim in one
respect: the cadastral engineer's name/phone/certificate are replaced with
"{scrubbed}". The code dictionaries (rosreestr_codes.json) are the site's own.
The one thing built inline is FakeSite below: the site's measured BEHAVIOUR
(one-use captcha, the check GET, a fresh image after every search) around
those real bodies, so the shared flow can be driven without a browser.
"""

import ast
import csv
import inspect
import io
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout
from dataclasses import asdict, fields

REPO = os.path.dirname(os.path.abspath(__file__))

# Hermetic before ANY project import can read the environment.
os.environ["ROSREESTR_ENV_FILE"] = os.devnull
for _k in ("TWOCAPTCHA_KEY", "ROSREESTR_CDP_ENDPOINT", "ROSREESTR_PROXY", "ROSREESTR_URL"):
    os.environ.pop(_k, None)

import captcha_solver  # noqa: E402
import cli  # noqa: E402
import diff_runs  # noqa: E402
import env_config  # noqa: E402
import lookup_flow  # noqa: E402
import output_writer  # noqa: E402
import proxy_pool  # noqa: E402
import rosreestr_api as ra  # noqa: E402
from output_writer import (EXIT_BLOCKED, EXIT_FETCH_FAILED, EXIT_NO_PRODUCTS,  # noqa: E402
                           EXIT_OK, EXIT_PARTIAL, QueryOutcome, Record, finish_run)

ENGINES = {}
SKIPPED_GROUPS = []
for _name in ("playwright_scraper", "puppeteer_scraper", "selenium_scraper"):
    try:
        ENGINES[_name] = __import__(_name)
    except ImportError as exc:
        SKIPPED_GROUPS.append(f"{_name} ({exc.name or exc})")

PASSED, FAILED, SKIPPED = [], [], []


def check(label, condition):
    (PASSED if condition else FAILED).append(label)
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")
    return bool(condition)


def skip(label, why):
    """A check that could NOT be made — never `check(label, True)`."""
    SKIPPED.append(f"{label} ({why})")
    print(f"  SKIP  {label} — {why}")
    return False


def eq(label, actual, expected):
    ok = actual == expected
    return check(label if ok else f"{label} (got {actual!r}, expected {expected!r})", ok)


with open(os.path.join(REPO, "fixtures.json"), encoding="utf-8") as _f:
    FX = json.load(_f)

KN = "77:01:0001044:3030"


def found_records():
    kind, data = ra.classify_on(FX["on_found"]["status"], FX["on_found"]["body"])
    return kind, ra.parse_on(data, query=KN, index=1)


# ---------------------------------------------------------------------------
# 1. Reading the site's answers — VALUES on real fixtures, not coverage
# ---------------------------------------------------------------------------
def check_full_record_values():
    print("\n[a full record, POST /account-back/on]")
    kind, rows = found_records()
    eq("a 200 with elements is an answer", kind, ra.ANSWERED)
    eq("one element, one row", len(rows), 1)
    r = rows[0]
    eq("sku is the cadastral number", r.sku, KN)
    eq("title is the site's readableAddress", r.title,
       "Москва, Тверской, ул. Тверская, д. 13, пом. III")
    eq("price is the cadastral value", r.price, 123431740.67)
    eq("currency is RUB, from the site's own '(руб)' label", r.currency, "RUB")
    eq("category decoded through OBJECT_TYPE_CODES", r.category, "Помещение")
    eq("object_type_code kept verbatim", r.object_type_code, "002001003000")
    eq("status '1' reads actual", r.status, "actual")
    eq("area from mainCharacters", (r.area, r.area_unit), (479.8, "кв.м"))
    eq("main characteristics kept whole", r.main_characteristics,
       [{"type": "Площадь", "value": 479.8, "unit": "кв.м"}])
    eq("reg_date: epoch ms -> the UTC calendar date", r.reg_date, "2013-03-22")
    eq("cadastral value determination date", r.cad_cost_determination_date, "2025-01-01")
    eq("purpose decoded (room purpose)", r.purpose, "Нежилое")
    eq("the floor a room is on", r.level_floor, "5")
    eq("old numbers kept with their kind", r.old_numbers,
       [{"type": "Инвентарный номер", "number": "III"}])
    eq("an empty rights list is null, not []", r.rights, None)
    eq("page/position number the query and the row", (r.page, r.position), (1, 1))
    check("url links the object through the page's own ?cadNumber=",
          r.url == ra.PAGE_URL + "?cadNumber=77%3A01%3A0001044%3A3030")
    eq("detail_level says it is the full record", r.detail_level, "full")
    # A record with no cadastral value must not ASSERT a currency. Found by a
    # planted fault this suite first let through (currency="RUB" always).
    el = json.loads(FX["on_found"]["body"])["elements"][0]
    el["cadCost"] = None
    bare = ra.record_from_element(el, query=KN, index=1, position=1)
    eq("no cadastral value -> price AND currency null", (bare.price, bare.currency), (None, None))
    el["cadCost"] = ""
    eq("an empty-string value is absent too, not 0",
       ra.record_from_element(el, query=KN, index=1, position=1).price, None)


def check_personal_data_is_never_read():
    print("\n[personal data]")
    names = {f.name for f in fields(Record)}
    check("no column carries the cadastral engineer (name/phone/certificate)",
          not any("eng" in n.lower() or "phone" in n.lower() or "fio" in n.lower()
                  for n in names))
    body = json.loads(FX["on_found"]["body"])["elements"][0]
    check("the fixture's engineer fields are scrubbed placeholders",
          all(body.get(k) in (None, "{scrubbed}") for k in
              ("cadEngFIO", "cadEngPhone", "cadEngCertNumber")))
    # Pattern, not the old literal: a future capture's phone is caught too.
    phone = re.compile(r"(?<!\d)(?:\+7|8)\d{10}(?!\d)")
    for name in ("fixtures.json", "sample_output.json", "sample_output.csv"):
        text = open(os.path.join(REPO, name), encoding="utf-8").read()
        check(f"{name} holds no Russian phone number", not phone.search(text))
    # Planted, to prove the scrubber and the pattern both bite.
    planted = json.loads(FX["on_found"]["body"])
    planted["elements"][0]["cadEngPhone"] = "89991234567"
    import make_fixtures
    check("make_fixtures scrubs a planted engineer phone",
          "89991234567" not in make_fixtures.scrub_on_body(json.dumps(planted)))


def check_answer_classification():
    print("\n[what an answer means]")
    eq("406 'Wrong captcha' (measured)", ra.classify_on(406, FX["on_wrong_captcha"]["body"])[0],
       ra.WRONG_CAPTCHA)
    eq("the 'not found' answer is an ANSWER", ra.classify_on(200, FX["on_empty"]["body"])[0],
       ra.ANSWERED)
    eq("...with no rows", ra.parse_on(json.loads(FX["on_empty"]["body"]), query="x", index=1), [])
    eq("403 is a refusal", ra.classify_on(403, "<html>")[0], ra.REFUSED)
    eq("429 is a refusal", ra.classify_on(429, "")[0], ra.REFUSED)
    eq("502 is a server error", ra.classify_on(502, "")[0], ra.SERVER_ERROR)
    eq("a 200 HTML page is unreadable, never 'no objects'",
       ra.classify_on(200, "<html>Личный кабинет</html>")[0], ra.UNREADABLE)
    eq("a 200 JSON without `elements` is unreadable too",
       ra.classify_on(200, '{"count": 0}')[0], ra.UNREADABLE)
    eq("an error body with the wrong status still reads as a wrong captcha",
       ra.classify_on(400, '{"error":"Wrong captcha"}')[0], ra.WRONG_CAPTCHA)


def check_address_search():
    print("\n[the free address search]")
    rows = ra.parse_address_search(FX["address_search"]["body"], query="q", index=3)
    eq("100 lines (the measured answer)", len(rows), 100)
    kinds = {}
    for r in rows:
        kinds[r.list_kind] = kinds.get(r.list_kind, 0) + 1
    eq("list_kind verbatim: 90 FLAT, 6 OKS, 4 PARCEL", kinds, {"FLAT": 90, "OKS": 6, "PARCEL": 4})
    eq("PARCEL is the one kind mapped (a land plot)",
       {r.category for r in rows if r.list_kind == "PARCEL"}, {"Земельный участок"})
    eq("OKS and FLAT are NOT guessed into a category",
       {r.category for r in rows if r.list_kind != "PARCEL"}, {None})
    eq("actual true/false -> actual / not_actual (89 / 11)",
       (sum(r.status == "actual" for r in rows), sum(r.status == "not_actual" for r in rows)),
       (89, 11))
    check("every line is a list line with no price", all(
        r.detail_level == "list" and r.price is None and r.currency is None for r in rows))
    eq("positions are 1..100 on the query's page", [r.position for r in rows], list(range(1, 101)))
    eq("the address URL is built byte-for-byte as the page built it",
       ra.address_search_url("Москва, ул. Тверская, д. 13"), FX["address_search"]["url"])
    eq("objType is always 'all' — the site ignores every other value (measured)",
       "objType=all" in ra.address_search_url("x"), True)
    eq("the cap constant is the measured one", ra.ADDRESS_CAP, 100)


def check_numbers_and_codes():
    print("\n[cadastral numbers and codes]")
    for good in (KN, "50:21:0000000:1", "1:1:1:1", " 77:01:0001044:3030 "):
        check(f"accepted: {good!r}", ra.is_cad_number(good))
    for bad in ("Москва, Тверская 13", "77:01:0001044", "77-01-0001044-3030", "", "77:01:0001044:30a"):
        check(f"refused: {bad!r}", not ra.is_cad_number(bad))
    eq("NBSP and spaces are stripped", ra.normalise_cad_number(" 77:01: 0001044:3030 "), KN)
    check("the front end's routing rule: letters go to the address search",
          ra.goes_to_address_search("ул. Тверская 13") and not ra.goes_to_address_search(KN))
    eq("the page's own /on body", ra.on_request_body(" " + KN, "ду76у"),
       {"filterType": "cadastral", "cadNumbers": [KN], "captcha": "ду76у"})
    eq("the request body matches the one the page sent (fixture)",
       ra.on_request_body(KN, "ду76у"), json.loads(FX["on_found"]["request"]))
    eq("nine object kinds — the brief's 'all categories'", len(ra.object_types()), 9)
    eq("a kind by name, any case", ra.object_type_code("помещение"), "002001003000")
    eq("a kind by code", ra.object_type_code("002001009000"), "002001009000")
    eq("an unknown kind is refused, not guessed", ra.object_type_code("Квартира"), None)
    eq("an unknown code decodes to itself, not to None",
       ra.decode("OBJECT_TYPE_CODES", "999"), "999")
    eq("a Cyrillic captcha answer is URL-quoted for its check",
       ra.captcha_check_url("ду76у"), ra.API + "/captcha/%D0%B4%D1%8376%D1%83")
    eq("a bad epoch is None, not a crash", ra._date("x"), None)


def check_page_recognition():
    print("\n[recognising the page]")
    service = '<div><button disabled id="realestateobjects-search">НАЙТИ</button></div>'
    check("the search form is recognised by its button id", ra.is_service_page(service))
    check("Chromium's error page is not the service",
          not ra.is_service_page("<html><title>lk.rosreestr.ru</title>ERR_PROXY_CONNECTION_FAILED</html>"))
    real_img = ('<img alt="captcha" class="rros-ui-lib-captcha-content-img" '
                'src="blob:https://lk.rosreestr.ru/e0651bea-21a4-4a6b-8030-d0203f5c246a">'
                '<input class="rros-ui-lib-captcha-input" id="captcha" name="captcha">')
    check("the site's image captcha is recognised with a blob: src (measured markup)",
          ra.has_image_captcha(real_img))
    check("the selector the engines use matches that markup by alt",
          "img[alt='captcha']" in ra.SEL_CAPTCHA_IMAGE)
    injected = ('<script src="chrome-extension://kjmkgkdkpedkejedfhmfcenooemhbpbo/content/'
                'captcha/turnstile/hunter.js" data-ts-input="cf-turnstile-response"></script>'
                '<script src="chrome-extension://kjmkgkdkpedkejedfhmfcenooemhbpbo/content/'
                'captcha/recaptcha/hunter.js"></script>')
    check("the Scraping Browser's injected hunters are not a captcha",
          captcha_solver.detect_in_html(injected, ra.PAGE_URL) is None
          and not ra.has_image_captcha(injected))


# ---------------------------------------------------------------------------
# 2. The output contract
# ---------------------------------------------------------------------------
def _outcome(i, n_rows=1, answered=True, **kw):
    rows = [Record(sku=f"77:01:0001044:{i}{j}", page=i, position=j + 1) for j in range(n_rows)]
    return QueryOutcome(index=i, query=str(i), answered=answered,
                        records=rows if answered else [], **kw)


def _finish(outcomes, requested, allow_empty=False, prefix=None):
    out = prefix or os.path.join(tempfile.mkdtemp(), "o")
    with redirect_stdout(io.StringIO()):
        rc = finish_run(outcomes, out, "both", allow_empty, mode="cadastral",
                        engine="test", queries_requested=requested)
    meta = (json.load(open(out + ".meta.json")) if os.path.exists(out + ".meta.json") else None)
    return rc, meta, out


def check_exit_codes():
    print("\n[exit codes and status — shared by every engine]")
    rc, meta, _ = _finish([_outcome(1), _outcome(2)], 2)
    eq("every query answered with rows: 0, complete", (rc, meta["status"]), (EXIT_OK, "complete"))
    rc, meta, _ = _finish([_outcome(1), _outcome(2, 0)], 2)
    eq("one 'not found' among them is still complete", (rc, meta["status"], meta["queries_not_found"]),
       (EXIT_OK, "complete", [2]))
    rc, meta, _ = _finish([_outcome(1, 0), _outcome(2, 0)], 2)
    eq("everything answered 'not found': 4, and NOTHING written", (rc, meta), (EXIT_NO_PRODUCTS, None))
    rc, meta, out = _finish([_outcome(1, 0)], 1, allow_empty=True)
    eq("...unless --allow-empty: 0, complete, an empty table",
       (rc, meta["status"], os.path.exists(out + ".csv")), (EXIT_OK, "complete", True))
    with open(out + ".csv", newline="", encoding="utf-8") as f:
        eq("an empty CSV still carries its header", next(csv.reader(f)),
           [fl.name for fl in fields(Record)])
    rc, meta, _ = _finish([_outcome(1), _outcome(2, answered=False, reason="lookup_no_answer")], 2)
    eq("rows, then a failure: 6, partial, the failure named by input position",
       (rc, meta["status"], [f["index"] for f in meta["queries_failed"]]), (EXIT_PARTIAL, "partial", [2]))
    rc, meta, _ = _finish([_outcome(1, answered=False, reason="page_fetch_raised")], 1)
    eq("nothing obtained: 5, no file", (rc, meta), (EXIT_FETCH_FAILED, None))
    rc, meta, _ = _finish([_outcome(1, answered=False, reason="captcha_rejected", blocked=True)], 1)
    eq("nothing obtained because the gate held: 3", (rc, meta), (EXIT_BLOCKED, None))
    rc, meta, _ = _finish([_outcome(1, 0), _outcome(2, answered=False, reason="x")], 2)
    eq("'not found' + a failure and no rows: 5 — a 6 would promise a file", rc, EXIT_FETCH_FAILED)
    rc, meta, _ = _finish([_outcome(1)], 3)
    eq("queries the engine never attempted make it partial",
       (rc, meta["status"], meta["stop_reason"]), (EXIT_PARTIAL, "partial", "queries_unattempted"))
    rc, meta, _ = _finish([], 2)
    eq("a run that never started: 5", rc, EXIT_FETCH_FAILED)
    # A failed run leaves the previous good output untouched.
    prefix = os.path.join(tempfile.mkdtemp(), "keep")
    _finish([_outcome(1)], 1, prefix=prefix)
    before = open(prefix + ".json", encoding="utf-8").read()
    _finish([_outcome(1, answered=False, reason="x")], 1, prefix=prefix)
    eq("a failed run does not overwrite the last good output",
       open(prefix + ".json", encoding="utf-8").read(), before)
    rc, meta, _ = _finish([_outcome(2), _outcome(1)], 2)
    eq("rows are merged in INPUT order, not arrival order", meta["records"], 2)
    dup = [_outcome(1), QueryOutcome(index=2, query="2", answered=True,
                                     records=[Record(sku="77:01:0001044:10")])]
    rc, meta, _ = _finish(dup, 2)
    eq("the same object from two queries is one row", (meta["records"], meta["duplicates_dropped"]), (1, 1))
    check("the sidecar records WHAT was asked", meta["queries"] == ["1", "2"] and len(meta["queries_sha256"]) == 64)


def check_csv_safety():
    print("\n[CSV]")
    eq("a formula-looking address is neutralised", output_writer.csv_safe("=HYPERLINK(1)"), "'=HYPERLINK(1)")
    eq("a number keeps its type", output_writer.csv_safe(-1.5), -1.5)
    eq("a list cell is JSON text", output_writer._cell([{"a": 1}]), '[{"a": 1}]')
    out = os.path.join(tempfile.mkdtemp(), "c.csv")
    output_writer.write_csv([Record(sku="1", title="+7 test", rights=[{"number": "n"}])], out)
    row = next(csv.DictReader(open(out, encoding="utf-8")))
    eq("in the file too", (row["title"], row["rights"]), ("'+7 test", '[{"number": "n"}]'))


# ---------------------------------------------------------------------------
# 3. The shared flow, driven against the site's measured behaviour
# ---------------------------------------------------------------------------
# The suite's clock: lookup_flow reads it through lookup_flow._clock, and a
# FakeSite's wait() advances it, so a deadline passes the moment the flow has
# "waited" long enough — no check spins on the real clock.
_FAKE_NOW = [0.0]
lookup_flow._clock = lambda: _FAKE_NOW[0]


class FakeSite:
    """lookup_flow's session contract over the site as MEASURED: a one-use
    captcha checked by GET /captcha/{text}, the real /on bodies, a new image
    after every search, a field that keeps the spent answer after a search."""

    needs_form = True

    def __init__(self, known=(KN,), autosolve=False, autosolve_fills=False, typing=False):
        self.net, self.n = [], 0
        self.image, self.spent = 1, False
        self.cap = self.query = ""
        self.known = set(known)
        self.autosolve, self.autosolve_events = autosolve, []
        self.autosolve_fills, self.typing = autosolve_fills, typing
        self.clicks = self.refreshes = 0

    def _push(self, u, s, body=None):
        self.n += 1
        self.net.append({"n": self.n, "u": u, "s": s, "body": body})

    def open_service(self):
        self._push("/account-back/captcha.png", 200)
        return 200, '<button id="realestateobjects-search">'

    def goto(self, url):
        return 200, "<html></html>"

    def html(self):
        return ""

    def net_entries(self):
        return list(self.net)

    def captcha_value(self):
        if self.autosolve_fills and not self.cap:
            self.set_captcha(f"img{self.image}")
        return self.cap

    def captcha_image(self):
        return f"img{self.image}".encode()

    def set_captcha(self, text):
        self.cap = text
        if not text:
            return
        if self.typing:     # a person typing: the page checks every prefix
            for i in range(1, len(text)):
                self._push("/account-back/captcha/" + text[:i], 403)
        ok = text == f"img{self.image}" and not self.spent
        self._push("/account-back/captcha/" + text, 200 if ok else 403)

    def refresh_captcha(self):
        self.refreshes += 1
        self.image += 1
        self.spent = False
        self._push("/account-back/captcha.png", 200)

    def set_query(self, t):
        self.query = t

    def search_enabled(self):
        return bool(self.cap and self.query)

    def click_search(self):
        self.clicks += 1
        ok = self.cap == f"img{self.image}" and not self.spent
        if not ok:
            self._push("/account-back/on", 406, FX["on_wrong_captcha"]["body"])
        elif self.query in self.known:
            self._push("/account-back/on", 200, FX["on_found"]["body"].replace(KN, self.query))
        else:
            self._push("/account-back/on", 200, FX["on_empty"]["body"])
        self.spent = True
        self.image += 1
        self.spent = False
        self._push("/account-back/captcha.png", 200)

    def fetch_text(self, url):
        if url.startswith("http"):
            raise RuntimeError("TypeError: Failed to fetch (cross-origin)")
        if "/address/search" in url:
            return 200, FX["address_search"]["body"]
        return 404, None

    def wait(self, s):
        _FAKE_NOW[0] += s

    def runtime_captcha_info(self):
        return None

    def inject_token(self, t):
        pass

    def dump(self, d, t):
        pass

    def close(self):
        pass


class FakeSolver:
    def __init__(self, wrong=0):
        self.wrong, self.calls, self.reports = wrong, 0, []

    def solve_image(self, key, image):
        self.calls += 1
        text = image.decode()
        if self.wrong > 0:
            self.wrong -= 1
            text = "bad"
        return captcha_solver.ImageSolution(text=text, task_id=self.calls)

    def report(self, key, task_id, correct):
        self.reports.append((task_id, correct))


_last_meta_path = [None]


def run_fake(argv, site, solver, key="k" * 32):
    extra = ["--out", os.path.join(tempfile.mkdtemp(), "o"), "--delay", "0", "--retry-delay", "0"]
    if key:
        extra += ["--twocaptcha-key", key]
    with redirect_stdout(io.StringIO()):
        args = cli.parse_args("test", "", argv + extra)
    orig = lookup_flow.Solver
    lookup_flow.Solver = lambda api_key: orig(api_key=api_key, solve_image=solver.solve_image,
                                              report=solver.report)
    try:
        with redirect_stdout(io.StringIO()):
            rc = cli.run(args, lambda a, p: site, "test")
    finally:
        lookup_flow.Solver = orig
    meta_path = args.out + ".meta.json"
    _last_meta_path[0] = meta_path
    return rc, (json.load(open(meta_path)) if os.path.exists(meta_path) else None)


def check_flow_on_measured_behaviour():
    print("\n[the shared flow, against the site's measured behaviour]")
    site, sol = FakeSite(known=(KN, "77:01:0001044:2981")), FakeSolver()
    rc, m = run_fake(["--cad-number", KN, "--cad-number", "77:01:0001044:2981",
                      "--cad-number", "77:01:0001044:999999"], site, sol)
    eq("three numbers: 2 found, 1 not found, complete, exit 0",
       (rc, m["status"], m["records"], m["queries_not_found"]), (0, "complete", 2, [3]))
    eq("one paid solve per number, and the counter IS the bill",
       (m["captcha_solves"], sol.calls), (3, 3))
    eq("three lookups reached the site, one per number", site.clicks, 3)

    site, sol = FakeSite(), FakeSolver(wrong=1)
    rc, m = run_fake(["--cad-number", KN], site, sol)
    eq("a wrong answer is reported incorrect, then a fresh image is solved",
       (rc, m["captcha_rejected"], sol.reports), (0, 1, [(1, False)]))
    check("...and the fresh image came from a refresh", site.refreshes >= 1)

    site, sol = FakeSite(), FakeSolver(wrong=99)
    rc, m = run_fake(["--cad-number", KN, "--captcha-attempts", "2", "--retries", "0"], site, sol)
    eq("a captcha the site keeps refusing: exit 3, nothing written", (rc, m), (EXIT_BLOCKED, None))
    eq("...after exactly --captcha-attempts paid solves", sol.calls, 2)

    site, sol = FakeSite(), FakeSolver(wrong=99)
    rc, m = run_fake(["--cad-number", KN, "--cad-number", "77:01:0001044:1",
                      "--max-solves", "3"], site, sol)
    eq("--max-solves is a hard cap across the run", sol.calls, 3)

    # With the DEFAULT --retries (1): an earlier version bought attempts x
    # (retries + 1) solves for one refused number and starved the next one.
    # The check above used --retries 0 and passed for the wrong reason.
    site, sol = FakeSite(known=(KN, "77:01:0001044:2981")), FakeSolver(wrong=3)
    rc, m = run_fake(["--cad-number", KN, "--cad-number", "77:01:0001044:2981"], site, sol)
    eq("a refused number costs --captcha-attempts, not attempts x rounds",
       (sol.calls, m["captcha_solves"]), (4, 4))
    eq("...and the next number still gets its own attempts and is answered",
       (rc, m["status"], [f["index"] for f in m["queries_failed"]]), (EXIT_PARTIAL, "partial", [1]))

    site, sol = FakeSite(), FakeSolver()
    rc, m = run_fake(["--cad-number", KN], site, sol, key=None)
    eq("no key: nothing is bought, the lookup is blocked (3)", (rc, sol.calls), (EXIT_BLOCKED, 0))

    site, sol = FakeSite(autosolve=True, autosolve_fills=True), FakeSolver()
    rc, m = run_fake(["--cad-number", KN, "--cad-number", "77:01:0001044:2981"], site, sol)
    eq("auto-solve answered: no paid solve at all", (rc, sol.calls, m["autosolved"]), (0, 0, 2))

    site, sol = FakeSite(autosolve=True, autosolve_fills=False), FakeSolver()
    rc, m = run_fake(["--cad-number", KN, "--autosolve-wait", "0.01"], site, sol)
    eq("auto-solve gets its turn first, then 2Captcha pays", (rc, sol.calls), (0, 1))

    site, sol = FakeSite(typing=True), FakeSolver()
    rc, m = run_fake(["--cad-number", KN, "--cad-number", "77:01:0001044:2981"], site, sol)
    eq("typed key by key, the prefix checks' 403s are not the verdict",
       (rc, m["captcha_rejected"]), (0, 0))

    site, sol = FakeSite(), FakeSolver()
    rc, m = run_fake(["--mode", "address", "--address", "Москва, ул. Тверская, д. 13"], site, sol)
    eq("address: 100 lines, no captcha, same-origin fetch",
       (rc, m["records"], sol.calls), (0, 100, 0))
    eq("...and flagged as hitting the site's cap", m["address_capped"], [1])

    site, sol = FakeSite(), FakeSolver()
    rc, m = run_fake(["--mode", "address", "--address", "Москва, ул. Тверская, д. 13",
                      "--list-kind", "PARCEL"], site, sol)
    eq("--list-kind filters client-side; address_results is what the site served",
       (m["records"], m["address_results"]), (4, 100))

    site, sol = FakeSite(), FakeSolver()
    rc, m = run_fake(["--mode", "address", "--address", "Москва, ул. Тверская, д. 13",
                      "--details", "--max-objects", "2"], site, sol)
    eq("--details: the full records REPLACE their list lines",
       (m["records"], m["captcha_solves"]), (100, 2))

    # Found by the 2026-09-29 audit; each of these passed silently before.
    site, sol = FakeSite(known=tuple(f"77:01:0001044:{n}" for n in range(2900, 3100))), FakeSolver()
    rc, m = run_fake(["--mode", "address", "--address", "A", "--address", "B",
                      "--details", "--max-objects", "2", "--max-solves", "1"], site, sol)
    eq("--details stopped by the budget is NOT 'completed', and the unvisited address counts",
       (rc, m["status"], m["stop_reason"], m["queries_requested"], m["queries_attempted"]),
       (EXIT_PARTIAL, "partial", "captcha_budget_exhausted", 4, 3))

    class TwoLines(FakeSite):
        def fetch_text(self, url):
            if "/address/search" in url:
                return 200, json.dumps([{"cadnum": KN, "full_name": "a", "actual": True, "type": "FLAT"},
                                        {"cadnum": "77:01:0001044:2981", "full_name": "b",
                                         "actual": True, "type": "OKS"}])
            return super().fetch_text(url)
    site, sol = TwoLines(known=(KN, "77:01:0001044:2981")), FakeSolver()
    rc, m = run_fake(["--mode", "address", "--address", "A", "--details", "--max-objects", "5"], site, sol)
    eq("an address whose lines ALL became full records is not 'not found'",
       (rc, m["queries_not_found"], m["records"]), (0, [], 2))
    rows = json.load(open(re.sub(r"\.meta\.json$", ".json", _last_meta_path[0])))
    eq("...and the full records sit in the list's own order",
       [(r["sku"], r["detail_level"]) for r in rows],
       [(KN, "full"), ("77:01:0001044:2981", "full")])
    check("...and the input digest ignores the planned detail lookups",
          m["queries"] == ["A"])

    eq("...and each full record keeps its LIST line's page, position and kind",
       [(r["page"], r["position"], r["list_kind"]) for r in rows], [(1, 1, "FLAT"), (1, 2, "OKS")])

    class Flaky(FakeSolver):
        def solve_image(self, key, image):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("createTask failed: ERROR_NO_SLOT_AVAILABLE")
            return captcha_solver.ImageSolution(text=image.decode(), task_id=self.calls)
    site, sol = FakeSite(), Flaky()
    rc, m = run_fake(["--cad-number", KN], site, sol)
    eq("one solver error spends one attempt, not the whole query", (rc, sol.calls), (0, 2))

    site, sol = FakeSite(), FakeSolver(wrong=99)
    rc, m = run_fake(["--mode", "address", "--address", "A", "--details", "--max-objects", "1",
                      "--captcha-attempts", "1"], site, sol)
    check("a failed --details lookup is marked derived — it is not an input line",
          m and [f["derived"] for f in m["queries_failed"]] == [True])

    rc, m = run_fake(["--mode", "address", "--address", "A", "--list-kind", "PARCEL",
                      "--allow-empty"], TwoLines(), FakeSolver())
    eq("--list-kind that keeps nothing reads as 'not found' for that address",
       (rc, m["queries_not_found"], m["address_results"]), (0, [1], 2))

    class NoStatus(FakeSite):
        def goto(self, url):
            return None, "<html><title>x</title></html>"
    rc, m = run_fake(["--mode", "scan", "--autosolve-wait", "0"], NoStatus(), FakeSolver())
    eq("--mode scan through an engine with no HTTP status (Selenium) is not a failure", rc, 0)

    class SomeCodes(FakeSite):
        def fetch_text(self, url):
            if "OBJECT_TYPE_CODES" in url:
                return 200, json.dumps([{"code": "002001001000", "value": "Земельный участок"}])
            return 404, None
    rc, m = run_fake(["--mode", "dictionaries"], SomeCodes(), FakeSolver())
    eq("a partial set of dictionaries is exit 6, not 0", rc, EXIT_PARTIAL)

    class Broken(FakeSite):
        def captcha_image(self):
            raise RuntimeError("Locator.screenshot: Timeout 60000ms exceeded")
    site, sol = Broken(), FakeSolver()
    rc, m = run_fake(["--cad-number", KN, "--cad-number", "77:01:0001044:2"], site, sol)
    eq("a driver error fails its query, not the run (5, no traceback)", rc, EXIT_FETCH_FAILED)
    eq("...and buys nothing", sol.calls, 0)


def check_budget_has_one_gate():
    print("\n[the paid-solve budget]")
    # "A cap that nothing enforces is a bill": every paid call site must sit
    # behind Budget.take. Counted in the source.
    src = inspect.getsource(lookup_flow)
    tree = ast.parse(src)
    paid, taken = 0, 0
    for fn in (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)):
        calls = [c for c in ast.walk(fn) if isinstance(c, ast.Call)
                 and isinstance(c.func, ast.Attribute)]
        p = sum(1 for c in calls if c.func.attr in ("solve_image", "solve"))
        t = sum(1 for c in calls if c.func.attr == "take")
        paid += p
        if p:
            check(f"{fn.name}: every paid call is behind budget.take()", t >= p)
        taken += t
    check(f"the budget is consulted ({taken}) at least once per paid call site ({paid})",
          paid >= 2 and taken >= paid)


# ---------------------------------------------------------------------------
# 4. The command line and configuration
# ---------------------------------------------------------------------------
def _parse(argv, env=None):
    saved = {k: os.environ.get(k) for k in env or {}}
    os.environ.update(env or {})
    try:
        with redirect_stdout(io.StringIO()):
            return cli.parse_args("test", "", argv)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _refused(argv, env=None):
    try:
        import contextlib
        with contextlib.redirect_stderr(io.StringIO()):
            _parse(argv, env)
    except SystemExit as e:
        return e.code == 2
    return False


def check_cli_contract():
    print("\n[command line]")
    cdp = "ws://u:p@cb.2captcha.com:9222"
    px = "http://u:p@ru.proxy.2captcha.com:2334"
    both = {"ROSREESTR_CDP_ENDPOINT": cdp, "ROSREESTR_PROXY": px}
    a = _parse(["--cad-number", KN], both)
    check("a .env with BOTH exits is not a usage error: the Scraping Browser wins",
          a.cdp_endpoint == cdp and not a.proxy)
    a = _parse(["--cad-number", KN, "--local"], both)
    check("--local takes the proxy and drops the endpoint", a.proxy == px and not a.cdp_endpoint)
    a = _parse(["--cad-number", KN, "--proxy", px], {"ROSREESTR_CDP_ENDPOINT": cdp})
    check("a TYPED proxy beats an endpoint from .env", a.proxy == px and not a.cdp_endpoint)
    check("both typed on the command line is refused",
          _refused(["--cad-number", KN, "--proxy", px, "--cdp-endpoint", cdp]))
    check("--fingerprint over the Scraping Browser is refused",
          _refused(["--cad-number", KN, "--fingerprint", "--cdp-endpoint", cdp]))
    check("--fp-tags with a list is refused (the API 400s)",
          _refused(["--cad-number", KN, "--fp-tags", "Windows,Chrome"]))
    check("an address given as a cadastral number is refused",
          _refused(["--cad-number", "Москва, Тверская 13"]))
    check("an unknown --list-kind is refused",
          _refused(["--mode", "address", "--address", "x", "--list-kind", "HOUSE"]))
    check("--mode cadastral with nothing to look up is refused", _refused([]))
    check("negative numbers are refused", _refused(["--cad-number", KN, "--delay", "-1"]))
    a = _parse(["--cad-number", KN, "--cad-number", KN, "--captcha-attempts", "2"])
    eq("the default solve cap is queries x attempts", a.max_solves, 4)
    a = _parse(["--cad-number", KN, "--solve-captcha", "never", "--twocaptcha-key", "k" * 32])
    check("--solve-captcha never drops the key, so nothing can be bought", a.twocaptcha_key is None)
    tmp = os.path.join(tempfile.mkdtemp(), "in.txt")
    open(tmp, "w", encoding="utf-8").write(f"﻿# header\n{KN}\n\n77:01:0001044:2981, # x\n")
    eq("--input: BOM, comments, blank lines and trailing commas",
       _parse(["--input", tmp]).queries, [KN, "77:01:0001044:2981"])


def check_rotation_survives_a_failed_launch():
    print("\n[per-query rotation]")
    px = "http://u:p@ru.proxy.2captcha.com:2334"
    tmp = os.path.join(tempfile.mkdtemp(), "px.txt")
    open(tmp, "w").write(px + "\n" + px.replace("ru.", "ru2.") + "\n")
    with redirect_stdout(io.StringIO()):
        args = cli.parse_args("test", "", ["--cad-number", KN, "--cad-number", "77:01:0001044:2981",
                                           "--proxy-file", tmp, "--proxy-rotate", "per-page",
                                           "--twocaptcha-key", "k" * 32, "--delay", "0",
                                           "--out", os.path.join(tempfile.mkdtemp(), "o")])
    opened, closed = [], []

    class Closing(FakeSite):
        def close(self):
            closed.append(self)

    def factory(a, p):
        opened.append(1)
        if len(opened) == 2:
            raise RuntimeError("Browser closed unexpectedly")
        return Closing()
    sol = FakeSolver()
    orig = lookup_flow.Solver
    lookup_flow.Solver = lambda api_key: orig(api_key=api_key, solve_image=sol.solve_image, report=sol.report)
    try:
        with redirect_stdout(io.StringIO()):
            rc = cli.run(args, factory, "test")
    finally:
        lookup_flow.Solver = orig
    meta = json.load(open(args.out + ".meta.json"))
    eq("a launch that fails on the next exit fails ITS query, not the run",
       (rc, meta["records"], [f["reason"] for f in meta["queries_failed"]]),
       (EXIT_PARTIAL, 1, ["browser_start_failed"]))
    eq("and the first session was closed exactly once", len(closed), 1)


def check_engine_without_cdp():
    print("\n[an engine that cannot use the Scraping Browser]")
    cdp = "ws://u:p@cb.2captcha.com:9222"
    px = "http://u:p@ru.proxy.2captcha.com:2334"
    saved = {k: os.environ.get(k) for k in ("ROSREESTR_CDP_ENDPOINT", "ROSREESTR_PROXY")}
    os.environ.update({"ROSREESTR_CDP_ENDPOINT": cdp, "ROSREESTR_PROXY": px})
    try:
        with redirect_stdout(io.StringIO()):
            a = cli.parse_args("selenium", "", ["--cad-number", KN], supports_cdp=False)
        check("the README's .env (endpoint + proxy) runs Selenium on the proxy",
              a.cdp_endpoint is None and a.proxy == px)
        import contextlib
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                cli.parse_args("selenium", "", ["--cad-number", KN, "--cdp-endpoint", cdp],
                               supports_cdp=False)
            refused = False
        except SystemExit as e:
            refused = e.code == 2
        check("a TYPED --cdp-endpoint on Selenium is a usage error", refused)
        os.environ.pop("ROSREESTR_PROXY", None)
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                cli.parse_args("selenium", "", ["--cad-number", KN], supports_cdp=False)
            refused = False
        except SystemExit as e:
            refused = e.code == 2
        check("Selenium with an endpoint and NO proxy is a usage error, not a doomed run", refused)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def check_engines_share_one_cli():
    print("\n[engine parity]")
    for name in ("playwright_scraper", "puppeteer_scraper", "selenium_scraper"):
        tree = ast.parse(open(os.path.join(REPO, f"{name}.py"), encoding="utf-8").read())
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name)
                 and n.func.value.id == "cli" and n.func.attr == "main"]
        check(f"{name} parses its flags through the shared cli.main", bool(calls))
        # Chrome's own `opts.add_argument("--headless=new")` is not a flag.
        parsers = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                   and isinstance(n.func, ast.Attribute) and n.func.attr == "add_argument"
                   and not (isinstance(n.func.value, ast.Name) and n.func.value.id in ("opts", "options"))]
        check(f"{name} adds no flag of its own (the sets cannot drift)", not parsers)
    # The session contract, DERIVED from the code that uses it (template
    # §26): every `session.<name>` the shared flow and the dispatcher touch.
    # A hand-written list drifts the day the loop uses a new operation.
    names = set()
    for mod in ("lookup_flow.py", "cli.py"):
        tree = ast.parse(open(os.path.join(REPO, mod), encoding="utf-8").read())
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                    and node.value.id == "session" and not node.attr.startswith("_")):
                names.add(node.attr)
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "getattr" and len(node.args) >= 2
                    and isinstance(node.args[0], ast.Name) and node.args[0].id == "session"
                    and isinstance(node.args[1], ast.Constant) and not node.args[1].value.startswith("_")):
                names.add(node.args[1].value)
    # `close` is called by the dispatcher through its holder of the live session.
    names.add("close")
    names = sorted(names - {"needs_form"})   # optional: only a one-shot transport sets it
    check(f"the contract is read off the flow itself ({len(names)} operations)", len(names) >= 18)
    documented = set(re.findall(r"^\s{4}([a-z_]+)(?:\(|\s)", lookup_flow.__doc__.split(
        "The session contract")[1].split("How a captcha")[0], re.M))
    eq("and the docstring's list names exactly those", sorted(documented), names)
    for name, cls in (("playwright_scraper", "PlaywrightSession"),
                      ("puppeteer_scraper", "PuppeteerSession"),
                      ("selenium_scraper", "SeleniumSession")):
        tree = ast.parse(open(os.path.join(REPO, f"{name}.py"), encoding="utf-8").read())
        klass = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == cls)
        defined = {n.name for n in klass.body if isinstance(n, ast.FunctionDef)}
        attrs = {t.attr for n in ast.walk(klass) for t in getattr(n, "targets", [])
                 if isinstance(t, ast.Attribute)} | {
                     t.id for n in klass.body if isinstance(n, ast.Assign)
                     for t in n.targets if isinstance(t, ast.Name)}
        missing = [m for m in names if m not in defined and m not in attrs]
        check(f"{cls} implements the whole session contract"
              + (f" (missing {missing})" if missing else ""), not missing)
    # The fake used above implements the same contract, or it proves nothing.
    missing = [m for m in names if not hasattr(FakeSite(), m)]
    check("the suite's FakeSite implements it too" + (f" (missing {missing})" if missing else ""),
          not missing)


def check_env_contract():
    print("\n[.env]")
    documented = set(env_config.documented_keys())
    eq(".env.example documents exactly the variables the code reads",
       documented, set(env_config.ENV_KEYS))
    # Round-trip a copied example through the REAL loader: every credential
    # reads as unset, the URL default stays usable.
    tmp = os.path.join(tempfile.mkdtemp(), ".env")
    shutil.copy(os.path.join(REPO, ".env.example"), tmp)
    saved = {k: os.environ.pop(k, None) for k in env_config.ENV_KEYS}
    try:
        env_config.load_env(tmp, override=True)
        for k, dest in env_config.ENV_KEYS.items():
            v = env_config.env_value(k)
            if dest in ("twocaptcha_key", "cdp_endpoint", "proxy"):
                check(f"a copied example's {k} reads as UNSET", v is None)
            else:
                eq(f"{k} keeps its real default", v, ra.PAGE_URL)
    finally:
        for k in env_config.ENV_KEYS:
            os.environ.pop(k, None)
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v
    check("the suite reads no real .env (ROSREESTR_ENV_FILE)",
          env_config.load_env() is None)
    out = subprocess.run([sys.executable, "env_config.py", "--help"], cwd=REPO,
                         capture_output=True, text=True,
                         env={**os.environ, "ROSREESTR_ENV_FILE": "/nonexistent/.env"})
    check("`env_config.py --help` answers without reading any .env",
          out.returncode == 0 and ".env file:" not in out.stdout)


def check_credentials_never_leak():
    print("\n[credentials in logs and errors]")
    secret = "ws://login:supersecret@cb.2captcha.com:9222"  # an allowlisted fixture userinfo
    masked = proxy_pool.redact_secret_patterns(f"connect {secret} failed; again {secret}")
    check("an endpoint's password is masked EVERY time it appears",
          "supersecret" not in masked and masked.count("cb.2captcha.com:9222") == 2)
    check("the host and port survive (which exit, not the secret)", "cb.2captcha.com:9222" in masked)
    check("a key in a query string is masked",
          "0123456789abcdef" not in proxy_pool.redact_secret_patterns(
              # Assembled: a literal 32-hex fixture would trip the repo's own scan.
              "https://api.2captcha.com/x?key=" + "0123456789abcdef" * 2))
    check("a bearer token is masked",
          "abcdefghijklmnop1234" not in proxy_pool.redact_secret_patterns(
              # Assembled, so the repo's own secret scan does not flag the fixture.
              "Authorization: " + "Bearer " + "abcdefghijklmnop" + "1234567890"))


def check_cdp_connect_policy():
    print("\n[Scraping Browser connect policy]")
    eq("a 500 on the WebSocket upgrade is retried",
       proxy_pool.cdp_retryable("WebSocket error: ws://***:***@cb.2captcha.com:9222/ 500 Internal Server Error"), True)
    eq("a 401 (expired credentials) is not", proxy_pool.cdp_retryable("401 Unauthorized"), False)
    eq("a number that merely CONTAINS 500 is not a 5xx", proxy_pool.cdp_retryable("port 5001 reset"), False)
    eq("...nor does 15000ms read as a 500, only as a timeout",
       proxy_pool.cdp_refusal_advice("Timeout 15000ms exceeded").startswith("no answer"), True)
    check("a 401 is explained as expired credentials",
          "expire" in proxy_pool.cdp_refusal_advice("<ws unexpected response> 401 Unauthorized"))


def check_diff_refuses_artefacts():
    print("\n[diff_runs]")
    d = tempfile.mkdtemp()

    def run(name, outcomes, requested):
        prefix = os.path.join(d, name)
        with redirect_stdout(io.StringIO()):
            finish_run(outcomes, prefix, "json", False, mode="cadastral", engine="t",
                       queries_requested=requested)
        return prefix + ".json"

    a = run("a", [_outcome(1), _outcome(2)], 2)
    b = run("b", [_outcome(1), _outcome(2)], 2)
    eq("two complete runs of the same input are comparable", diff_runs.check_comparable(a, b), [])
    c = run("c", [_outcome(1)], 1)
    check("a different input is refused — a missing number is not a removal",
          any("different questions" in p for p in diff_runs.check_comparable(a, c)))
    p = run("p", [_outcome(1), _outcome(2, answered=False, reason="x")], 2)
    check("a partial run is refused", any("partial" in x for x in diff_runs.check_comparable(a, p)))
    old = [asdict(Record(sku="1", price=1.0, detail_level="full"))]
    new = [asdict(Record(sku="1", price=2.0, detail_level="full"))]
    eq("a re-valuation is a change", len(diff_runs.diff_rows(old, new)["changed"]), 1)
    lst = [asdict(Record(sku="1", detail_level="list"))]
    eq("a list line vs a full record is not a change", diff_runs.diff_rows(lst, new)["changed"], [])
    lone = os.path.join(d, "lone.json")
    open(lone, "w").write("[]")
    check("a file with no sidecar is refused — nothing vouches for it",
          any("no .meta.json" in x for x in diff_runs.check_comparable(lone, b)))
    with open(a, "a", encoding="utf-8") as f:
        f.write(" ")
    check("a data file rewritten after its sidecar is refused",
          any("does not match" in x for x in diff_runs.check_comparable(a, b)))


# ---------------------------------------------------------------------------
# 5. Structural checks, family-wide
# ---------------------------------------------------------------------------
def _module_sources():
    for name in sorted(os.listdir(REPO)):
        if name.endswith(".py"):
            yield name, open(os.path.join(REPO, name), encoding="utf-8").read()
    for extra in ("tests/mock_site.py", "tests/engine_e2e.py", "tests/test_smoke.py"):
        path = os.path.join(REPO, extra)
        if os.path.exists(path):
            yield extra, open(path, encoding="utf-8").read()


def check_engine_imports_driver_at_module_level():
    print("\n[driver imports]")
    for name, driver in (("playwright_scraper", "playwright"), ("puppeteer_scraper", "pyppeteer"),
                         ("selenium_scraper", "selenium")):
        tree = ast.parse(open(os.path.join(REPO, f"{name}.py"), encoding="utf-8").read())
        found = any((isinstance(n, ast.ImportFrom) and (n.module or "").startswith(driver))
                    or (isinstance(n, ast.Import) and any(a.name.startswith(driver) for a in n.names))
                    for n in tree.body)
        check(f"{name} imports {driver} at module level", found)


def check_no_undefined_names():
    print("\n[undefined names]")
    import builtins
    for name, source in _module_sources():
        tree = ast.parse(source)
        bound = set(dir(builtins)) | {"__name__", "__file__", "__doc__"}
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    bound.add((alias.asname or alias.name).split(".")[0])
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                bound.add(node.name)
            elif isinstance(node, ast.arg):
                bound.add(node.arg)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
                bound.add(node.id)
            elif isinstance(node, ast.ExceptHandler) and node.name:
                bound.add(node.name)
            elif isinstance(node, ast.Global):
                bound.update(node.names)
        used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
        unknown = sorted(used - bound)
        check(f"{name}: every name it loads is bound" + (f" (unknown: {unknown})" if unknown else ""),
              not unknown)


def check_shared_calls_bind():
    print("\n[shared call signatures]")
    shared = {"rosreestr_api": ra, "lookup_flow": lookup_flow, "output_writer": output_writer,
              "captcha_solver": captcha_solver, "proxy_pool": proxy_pool,
              "env_config": env_config, "cli": cli}
    placeholder, bound_calls = object(), 0
    for name, source in _module_sources():
        tree = ast.parse(source)
        callables, modules, problems, local = {}, {}, [], set()
        for node in ast.walk(tree):
            if isinstance(node, ast.arg):
                local.add(node.arg)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                local.add(node.id)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in shared:
                for alias in node.names:
                    if not hasattr(shared[node.module], alias.name):
                        problems.append(f"imports {alias.name!r}, which {node.module} does not define")
                    elif callable(getattr(shared[node.module], alias.name)):
                        callables[alias.asname or alias.name] = getattr(shared[node.module], alias.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    as_ = alias.asname or alias.name
                    if alias.name in shared and as_ not in local:
                        modules[as_] = shared[alias.name]
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            target = None
            if isinstance(node.func, ast.Name):
                target = callables.get(node.func.id)
            elif (isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)
                  and node.func.value.id in modules):
                mod = modules[node.func.value.id]
                if not hasattr(mod, node.func.attr):
                    problems.append(f"line {node.lineno}: {node.func.value.id}.{node.func.attr} does not exist")
                    continue
                target = getattr(mod, node.func.attr)
            if target is None or isinstance(target, type) or not callable(target):
                continue
            if any(isinstance(a, ast.Starred) for a in node.args) or any(k.arg is None for k in node.keywords):
                continue
            try:
                inspect.signature(target).bind(*([placeholder] * len(node.args)),
                                               **{k.arg: placeholder for k in node.keywords})
                bound_calls += 1
            except TypeError as e:
                problems.append(f"line {node.lineno}: {getattr(target, '__name__', target)} {e}")
            except ValueError:
                continue
        check(f"{name}: every call into a shared module binds" + (f" — {problems}" if problems else ""),
              not problems)
    check(f"the binding check actually bound calls ({bound_calls})", bound_calls > 60)


def check_no_code_after_a_terminator():
    print("\n[unreachable code]")
    hits, scanned = [], 0
    for name, source in _module_sources():
        scanned += 1
        for node in ast.walk(ast.parse(source)):
            for fld in ("body", "orelse", "finalbody"):
                block = getattr(node, fld, None)
                if isinstance(block, list):
                    for i, stmt in enumerate(block[:-1]):
                        if isinstance(stmt, (ast.Return, ast.Raise, ast.Break, ast.Continue)):
                            hits.append(f"{name}:{block[i + 1].lineno}")
    check(f"no statement follows a return/raise/break/continue ({scanned} modules)"
          + (f" — {hits}" if hits else ""), not hits and scanned > 10)


def check_python_floor():
    print("\n[Python 3.9 grammar]")
    bad = []
    for name, source in _module_sources():
        try:
            ast.parse(source, filename=name, feature_version=(3, 9))
        except SyntaxError as e:
            bad.append(f"{name}: {e}")
    for extra in (".github/ci_checks.py",):
        try:
            ast.parse(open(os.path.join(REPO, extra), encoding="utf-8").read(), feature_version=(3, 9))
        except SyntaxError as e:
            bad.append(f"{extra}: {e}")
    check("every module parses under Python 3.9" + (f" — {bad}" if bad else ""), not bad)


def check_shipped_javascript_parses():
    print("\n[JavaScript that goes into the browser]")
    # A syntax error in an init script is SILENT: the browser logs it to a
    # console nobody reads and the page runs unpatched. That is exactly how
    # `--fingerprint` shipped doing nothing in two sibling repos ("( => {").
    node = shutil.which("node")
    import fingerprint_client
    snippets = {
        "NETWORK_HOOK_JS": ra.NETWORK_HOOK_JS,
        "CAPTCHA_DISCOVERY_JS": "(" + captcha_solver.CAPTCHA_DISCOVERY_JS + ")",
        "INJECT_TOKEN_FN": "(" + captcha_solver.INJECT_TOKEN_FN + ")",
        "INJECT_TOKEN_BODY": "function f(){" + captcha_solver.INJECT_TOKEN_BODY + "}",
        "fingerprint init script": fingerprint_client.playwright_init_script({}),
    }
    for mod, prefix in (("puppeteer_scraper", "_"), ("selenium_scraper", "_")):
        src = open(os.path.join(REPO, f"{mod}.py"), encoding="utf-8").read()
        for m in re.finditer(r'^(_[A-Z_]+(?:JS|BODY)) = """(.*?)"""', src, re.S | re.M):
            code = m.group(2)
            snippets[f"{mod}.{m.group(1)}"] = ("function f(){" + code + "}" if m.group(1).endswith("BODY")
                                               else "(" + code + ")")
    check(f"{len(snippets)} snippets collected", len(snippets) >= 9)
    if not node:
        skip("JavaScript syntax (node --check)", "node not installed; CI has it")
        return
    d = tempfile.mkdtemp()
    for label, code in snippets.items():
        path = os.path.join(d, "s.js")
        open(path, "w", encoding="utf-8").write(code)
        r = subprocess.run([node, "--check", path], capture_output=True, text=True)
        check(f"{label} parses" + ("" if r.returncode == 0 else f" — {r.stderr.strip()[:160]}"),
              r.returncode == 0)


def check_banned_wording():
    print("\n[wording]")
    banned = ["anti" + "detect", "cloud " + "browser", "gate." + "2prx.com", "ANTI" + "DETECT_LOCAL_API"]
    scanned, hits = 0, []
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in {".git", "__pycache__"}
                   and not os.path.exists(os.path.join(root, d, "pyvenv.cfg"))]
        for filename in files:
            if not filename.endswith((".py", ".md", ".yml", ".yaml", ".txt", ".example",
                                      ".toml", ".html", ".json")) \
                    and filename not in ("Dockerfile", ".gitignore", ".dockerignore"):
                continue
            text = open(os.path.join(root, filename), encoding="utf-8", errors="replace").read().lower()
            scanned += 1
            hits += [f"{os.path.relpath(os.path.join(root, filename), REPO)}: {p!r}"
                     for p in banned if p.lower() in text]
    check(f"no shipped file uses a banned product name ({scanned} files, this suite included)"
          + (f" — {hits}" if hits else ""), not hits and scanned > 20)


def check_dockerfile_copies_what_it_imports():
    print("\n[Dockerfile]")
    dockerfile = open(os.path.join(REPO, "Dockerfile"), encoding="utf-8").read()
    joined = re.sub(r"\\\s*\n", " ", dockerfile)
    copied = {t for line in re.findall(r"^COPY\s+(.+)$", joined, re.M)
              for t in line.split() if t.endswith((".py", ".json"))}
    local = {n[:-3] for n in os.listdir(REPO) if n.endswith(".py")}
    needed, queue = set(), ["playwright_scraper"]
    while queue:
        m = queue.pop()
        if m in needed:
            continue
        needed.add(m)
        for node in ast.walk(ast.parse(open(os.path.join(REPO, f"{m}.py"), encoding="utf-8").read())):
            if isinstance(node, ast.ImportFrom) and node.module in local:
                queue.append(node.module)
            elif isinstance(node, ast.Import):
                queue += [a.name for a in node.names if a.name in local]
    missing = ({f"{m}.py" for m in needed} | {"rosreestr_codes.json"}) - copied
    check("the image carries every module the entrypoint imports, and the code snapshot"
          + (f" (missing {sorted(missing)})" if missing else ""), not missing)
    check("and no test suite or fixtures", not ({"smoke_test.py", "fixtures.json"} & copied))
    check("and no .env", not re.search(r"^COPY\s+.*\.env(\s|$)", dockerfile, re.M))


def check_packaging_matches_the_tree():
    print("\n[packaging]")
    try:
        import tomllib
    except ImportError:
        skip("pyproject.toml cross-check", "tomllib needs 3.11; CI's newest leg runs it")
        return
    config = tomllib.load(open(os.path.join(REPO, "pyproject.toml"), "rb"))
    declared = set(config["tool"]["setuptools"]["py-modules"])
    on_disk = {n[:-3] for n in os.listdir(REPO) if n.endswith(".py")} - {"smoke_test"}
    eq("every module on disk is declared", on_disk - declared, set())
    eq("nothing declared is missing", declared - on_disk, set())

    def reqs(name):
        return sorted(line.split("#")[0].strip() for line in open(os.path.join(REPO, name), encoding="utf-8")
                      if line.strip() and not line.strip().startswith("#"))
    eq("dependencies match requirements.txt", sorted(config["project"]["dependencies"]), reqs("requirements.txt"))
    for engine in ("playwright", "puppeteer", "selenium"):
        eq(f"the {engine} extra matches its requirements file",
           sorted(config["project"]["optional-dependencies"][engine]), reqs(f"requirements-{engine}.txt"))


def check_ci_checks_are_one_implementation():
    print("\n[CI checks]")
    path = os.path.join(REPO, ".github", "ci_checks.py")
    r = subprocess.run([sys.executable, path, "--secret-check", "--sample-check"],
                       capture_output=True, text=True, cwd=REPO)
    check("`ci_checks.py --secret-check --sample-check` passes"
          + ("" if r.returncode == 0 else f" — {(r.stdout + r.stderr).strip().splitlines()[-1][:160]}"),
          r.returncode == 0)
    workflow = os.path.join(REPO, ".github", "workflows", "tests.yml")
    if not os.path.isdir(os.path.join(REPO, ".github", "workflows")):
        # Inside the Docker image there is no .github at all — only then.
        skip("the workflow calls ci_checks.py", "no .github/workflows in this tree")
        return
    text = open(workflow, encoding="utf-8").read()
    check("the workflow CALLS ci_checks.py rather than reimplementing it", "ci_checks.py" in text)
    check("the engine jobs run this suite with --e2e", "smoke_test.py --e2e" in text)


def check_supply_chain():
    print("\n[supply chain]")
    wf_dir = os.path.join(REPO, ".github", "workflows")
    if not os.path.isdir(wf_dir):
        skip("workflow pins", "no .github/workflows in this tree")
        return
    for name in sorted(os.listdir(wf_dir)):
        text = open(os.path.join(wf_dir, name), encoding="utf-8").read()
        uses = re.findall(r"uses:\s*([^\s#]+)", text)
        loose = [u for u in uses if not re.search(r"@[0-9a-f]{40}$", u)]
        check(f"{name}: every action is pinned by commit SHA"
              + (f" (not: {loose})" if loose else ""), not loose)
        code = "\n".join(ln for ln in text.splitlines() if not ln.strip().startswith("#"))
        installs = re.findall(r"pip install ([^\n]+)", code)
        bad = [i for i in installs if "--require-hashes" not in i or ".lock" not in i]
        check(f"{name}: pip installs only hash-checked locks" + (f" (not: {bad})" if bad else ""),
              not bad)
    # A pipe in a `run:` step hides the left side's failure unless pipefail
    # is on — and GitHub runs an unspecified shell as `bash -e` WITHOUT it.
    for name in sorted(os.listdir(wf_dir)):
        text = open(os.path.join(wf_dir, name), encoding="utf-8").read()
        for step in re.split(r"\n\s*- (?:name|uses):", text):
            code = "\n".join(ln for ln in step.splitlines()
                             if ln.strip() and not ln.strip().startswith("#"))
            m = re.search(r"run:\s*\|?(.*)", code, re.S)
            if not m or not re.search(r"python[^\n]*\|\s*tee", m.group(1)):
                continue
            check(f"{name}: a piped python step runs with pipefail",
                  "shell: bash" in code or "pipefail" in code)
    for txt, lock in (("requirements.txt", "requirements.lock"),
                      ("requirements-playwright.txt", "requirements-playwright.lock"),
                      ("requirements-puppeteer.txt", "requirements-puppeteer.lock"),
                      ("requirements-selenium.txt", "requirements-selenium.lock")):
        wanted = {re.split(r"[<>=!~\[ ]", line.strip())[0].lower()
                  for line in open(os.path.join(REPO, txt), encoding="utf-8")
                  if line.strip() and not line.startswith("#")}
        if txt != "requirements.txt":
            wanted |= {"requests"}
        pinned = set(re.findall(r"^([a-z0-9_.-]+)==", open(os.path.join(REPO, lock),
                                                           encoding="utf-8").read(), re.M | re.I))
        pinned = {x.lower() for x in pinned}
        missing = wanted - pinned
        check(f"{lock} pins everything {txt} asks for" + (f" (missing {missing})" if missing else ""),
              not missing)
        check(f"{lock} carries hashes", "--hash=sha256:" in open(os.path.join(REPO, lock), encoding="utf-8").read())


def check_sample_output():
    print("\n[sample output]")
    rows = json.load(open(os.path.join(REPO, "sample_output.json"), encoding="utf-8"))
    columns = [f.name for f in fields(Record)]
    check("the sample holds rows", bool(rows))
    check("its columns match Record, in order", all(list(r) == columns for r in rows))
    check("it holds a full record AND list lines",
          {r["detail_level"] for r in rows} == {"full", "list"})
    check("every row carries a cadastral number and the object's link",
          all(ra.is_cad_number(r["sku"]) and "?cadNumber=" in r["url"] for r in rows))
    # `*.json` is ignored (run output), so each data file the suite and the
    # image need must be negated — one was not, and was therefore neither
    # committable nor secret-scanned.
    if shutil.which("git") and os.path.isdir(os.path.join(REPO, ".git")):
        for name in ("fixtures.json", "rosreestr_codes.json", "sample_output.json",
                     "sample_output.csv", ".env.example"):
            r = subprocess.run(["git", "check-ignore", "-q", name], cwd=REPO)
            check(f"{name} is not git-ignored", r.returncode == 1)
        for name in (".env", ".env.bak", "live/dump.html", "rosreestr_objects.json"):
            r = subprocess.run(["git", "check-ignore", "-q", name], cwd=REPO)
            check(f"{name} IS git-ignored", r.returncode == 0)
    else:
        skip("git-ignore rules", "not a git checkout")


def check_engines_end_to_end(run_e2e):
    print("\n[engines, end to end against tests/mock_site.py]")
    for name in ("playwright_scraper", "puppeteer_scraper", "selenium_scraper"):
        engine = name.split("_")[0]
        if name not in ENGINES:
            skip(f"{engine} end to end", "engine library not installed")
            continue
        if not run_e2e:
            skip(f"{engine} end to end", "pass --e2e (needs a local browser)")
            continue
        r = subprocess.run([sys.executable, os.path.join(REPO, "tests", "engine_e2e.py"), engine],
                           capture_output=True, text=True, cwd=REPO, timeout=600)
        fails = [ln for ln in r.stdout.splitlines() if ln.startswith("FAIL")]
        check(f"{engine}: form, hook, captcha image, retry, answer, files, exit code"
              + ("" if r.returncode == 0 else f" — {(fails or (r.stdout + r.stderr).splitlines()[-3:])}"),
              r.returncode == 0)


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    logging.basicConfig(level=logging.CRITICAL)
    print("rosreestr-scraper offline suite")
    print("=" * 62)
    for skipped in SKIPPED_GROUPS:
        print(f"  SKIP  engine module {skipped}")
    groups = [check_full_record_values, check_personal_data_is_never_read,
              check_answer_classification, check_address_search, check_numbers_and_codes,
              check_page_recognition, check_exit_codes, check_csv_safety,
              check_flow_on_measured_behaviour, check_budget_has_one_gate,
              check_cli_contract, check_rotation_survives_a_failed_launch,
              check_engine_without_cdp, check_engines_share_one_cli, check_env_contract,
              check_credentials_never_leak, check_cdp_connect_policy,
              check_diff_refuses_artefacts,
              check_engine_imports_driver_at_module_level, check_no_undefined_names,
              check_shared_calls_bind, check_no_code_after_a_terminator, check_python_floor,
              check_shipped_javascript_parses, check_banned_wording,
              check_dockerfile_copies_what_it_imports, check_packaging_matches_the_tree,
              check_ci_checks_are_one_implementation, check_supply_chain, check_sample_output]
    # Every check_ function must be wired: one defined and never run is a
    # guard that looks present and is not (family §25).
    defined = {n for n, f in globals().items() if n.startswith("check_") and callable(f)}
    wired = {g.__name__ for g in groups} | {"check", "check_engines_end_to_end"}
    for group in groups:
        try:
            group()
        except Exception as e:  # noqa: BLE001 — a crashing group is a failure, loudly
            check(f"{group.__name__} ran without raising ({type(e).__name__}: {e})", False)
    check_engines_end_to_end("--e2e" in argv)
    check("every check_ function in this file is wired into main()"
          + (f" (unwired: {sorted(defined - wired)})" if defined - wired else ""),
          not (defined - wired))
    print("=" * 62)
    print(f"{len(PASSED)} passed, {len(FAILED)} failed, {len(SKIPPED)} skipped")
    for f in FAILED:
        print(f"  FAILED: {f}")
    for s in SKIPPED + [f"engine module {g}" for g in SKIPPED_GROUPS]:
        print(f"  skipped: {s}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
