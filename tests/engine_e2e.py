"""
tests/engine_e2e.py
-------------------
Drive ONE real engine, with a real local browser, against tests/mock_site.py.

    python3 tests/engine_e2e.py playwright|puppeteer|selenium

What it proves that nothing offline otherwise can: the engine types into the
real form ids, the network hook installs and records, the captcha image is
found and screenshotted, the button is clicked, the answer is read, a wrong
answer is refused and retried, a next query waits for its fresh image, and
the whole run writes the same files with the same exit code in every engine.

The paid solver is replaced by one that asks the mock for the answer; every
other line that runs is the shipped code. Exits 0 on success, 1 with the
first failed expectation otherwise. Needs no network, key or .env — and
refuses to read one (ROSREESTR_ENV_FILE points at nothing).
"""

import importlib
import json
import os
import sys
import tempfile
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

# Offline by construction: no .env, no credentials from the environment.
os.environ["ROSREESTR_ENV_FILE"] = os.devnull
for k in ("TWOCAPTCHA_KEY", "ROSREESTR_CDP_ENDPOINT", "ROSREESTR_PROXY", "ROSREESTR_URL"):
    os.environ.pop(k, None)

import captcha_solver  # noqa: E402
import lookup_flow  # noqa: E402
from mock_site import MockSite  # noqa: E402


def main(engine: str) -> int:
    mod = importlib.import_module(f"{engine}_scraper")
    failures = []

    def expect(cond, what):
        if not cond:
            failures.append(what)
            print(f"FAIL [{engine}] {what}")

    with MockSite(wrong_checks=1) as site:
        def fake_solve(key, image, **kw):
            expect(isinstance(image, bytes) and image[:4] == b"\x89PNG",
                   "captcha_image() returned a PNG screenshot")
            answer = urllib.request.urlopen(site.base + "/_test/answer").read().decode()
            return captcha_solver.ImageSolution(text=answer, task_id=len(answer))

        reports = []
        orig = lookup_flow.Solver
        lookup_flow.Solver = lambda api_key: orig(api_key=api_key, solve_image=fake_solve,
                                                  report=lambda k, t, ok: reports.append(ok))
        out = os.path.join(tempfile.mkdtemp(), "e2e")
        try:
            rc = mod.main(["--url", site.url, "--twocaptcha-key", "k" * 32,
                           "--cad-number", "77:01:0001044:3030",
                           "--cad-number", "77:01:0001044:999999",
                           "--out", out, "--delay", "0", "--retry-delay", "0",
                           "--autosolve-wait", "0", "--headless"])
        finally:
            lookup_flow.Solver = orig
        expect(rc == 0, f"exit code 0 (got {rc})")
        meta = json.load(open(out + ".meta.json")) if os.path.exists(out + ".meta.json") else {}
        rows = json.load(open(out + ".json")) if os.path.exists(out + ".json") else []
        expect(meta.get("status") == "complete", f"status complete (got {meta.get('status')})")
        expect(meta.get("queries_not_found") == [2], f"query 2 not found (got {meta.get('queries_not_found')})")
        expect(len(rows) == 1 and rows[0]["sku"] == "77:01:0001044:3030", "one row, the known object")
        expect(rows and rows[0]["price"] == 123431740.67 and rows[0]["currency"] == "RUB",
               "cadastral value and currency read")
        expect(meta.get("captcha_rejected") == 1 and False in reports,
               "the forced wrong check was rejected and reported")
        expect(len(site.lookups) == 2, f"exactly two lookups reached the site (got {len(site.lookups)})")
        expect(all(b.get("filterType") == "cadastral" for b in site.lookups),
               "the page's own request body was sent")

        # The free address search, same engine, same mock.
        out2 = os.path.join(tempfile.mkdtemp(), "addr")
        rc2 = mod.main(["--url", site.url, "--mode", "address", "--address",
                        "Москва, ул. Тверская, д. 13", "--out", out2, "--headless"])
        expect(rc2 == 0, f"address mode exit 0 (got {rc2})")
        rows2 = json.load(open(out2 + ".json")) if os.path.exists(out2 + ".json") else []
        expect(len(rows2) == 100, f"100 address lines (got {len(rows2)})")

    print(f"{engine}: " + ("OK" if not failures else f"{len(failures)} failure(s)"))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
