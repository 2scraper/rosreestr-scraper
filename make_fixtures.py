"""
make_fixtures.py
----------------
Cut the offline fixtures and the code-dictionary snapshot out of a live probe
log, scrubbing personal data on the way.

    python3 make_fixtures.py path/to/p3_log.json

Writes two files next to this script:

  fixtures.json         real `/account-back/...` response bodies, one per
                        shape the suite needs (a found object, an empty
                        answer, a rejected captcha, an address search, the
                        dictionaries)
  rosreestr_codes.json  the site's own code dictionaries (object type, land
                        category, permitted use, room and building purpose),
                        used to turn "002001003000" into "Помещение" without a
                        request. `python3 playwright_scraper.py --mode
                        dictionaries` refreshes the same data live.

The raw probe log is NOT committed: it lives outside the repo and carries the
session that fetched it. What is scrubbed, and why:

  cadEngFIO / cadEngPhone / cadEngCertNumber
      The name, phone and certificate number of the cadastral engineer who
      filed the object. The site shows them; republishing a private person's
      phone in a public repo is a different act, and no column here reads
      them (see output_writer.Record). Replaced with an obvious placeholder
      so the fixture keeps the SHAPE the parser must ignore.

Everything else is left byte-for-byte as the site sent it.
"""

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRUBBED = "{scrubbed}"
PERSONAL_FIELDS = ("cadEngFIO", "cadEngPhone", "cadEngCertNumber")


def scrub_on_body(text: str) -> str:
    data = json.loads(text)
    for el in data.get("elements") or []:
        for k in PERSONAL_FIELDS:
            if el.get(k) is not None:
                el[k] = SCRUBBED
    return json.dumps(data, ensure_ascii=False)


def main(argv) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    log = json.loads(Path(argv[1]).read_text(encoding="utf-8"))["log"]

    def first(pred):
        for r in log:
            if pred(r):
                return r
        raise SystemExit(f"no record in {argv[1]} matches the fixture being cut")

    def is_on(r):
        return r["u"].split("?")[0].endswith("/account-back/on")

    found = first(lambda r: is_on(r) and r["s"] == 200 and '"count":1' in r["body"])
    empty = first(lambda r: is_on(r) and r["s"] == 200 and '"count":0' in r["body"])
    wrong = first(lambda r: is_on(r) and r["s"] == 406)
    addr = first(lambda r: "/account-back/address/search" in r["u"] and r["s"] == 200)
    fixtures = {
        "_comment": "Real lk.rosreestr.ru responses captured 2026-09-29 over a "
                    "Russian exit. Not verbatim in one respect: the cadastral "
                    "engineer's name/phone/certificate are replaced with "
                    f"{SCRUBBED!r} (see make_fixtures.py).",
        "on_found": {"status": found["s"], "request": found["post"], "body": scrub_on_body(found["body"])},
        "on_empty": {"status": empty["s"], "request": empty["post"], "body": empty["body"]},
        "on_wrong_captcha": {"status": wrong["s"], "request": wrong["post"], "body": wrong["body"]},
        "address_search": {"status": addr["s"], "url": addr["u"], "body": addr["body"]},
    }
    (HERE / "fixtures.json").write_text(json.dumps(fixtures, ensure_ascii=False, indent=1) + "\n",
                                        encoding="utf-8")

    codes = {"_comment": "The site's own dictionaries, GET /account-back/dictionary/{NAME}"
                         "?sortKey=code, captured 2026-09-29. Refresh with --mode dictionaries."}
    for r in log:
        if "/account-back/dictionary/" in r["u"] and r["s"] == 200:
            name = r["u"].split("/dictionary/")[1].split("?")[0]
            codes[name] = {e["code"]: e["value"] for e in json.loads(r["body"])}
    (HERE / "rosreestr_codes.json").write_text(json.dumps(codes, ensure_ascii=False, indent=1) + "\n",
                                               encoding="utf-8")
    print(f"wrote fixtures.json and rosreestr_codes.json ({len(codes) - 1} dictionaries)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
