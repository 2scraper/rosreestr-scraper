#!/usr/bin/env python3
"""
diff_runs.py
-------------
Compare two outputs of this project and report what changed, keyed on `sku`
— the cadastral number.

    python3 diff_runs.py --old objects.2026-09-01.json --new objects.2026-10-01.json

The typical use is monitoring a fixed list of objects: the same --input file
looked up on a schedule, each run under a dated --out, each diffed against the
previous one. What changes on a real-estate object and is worth an alert:

  price (the cadastral value) and its two dates — a re-valuation
  status                    — "actual" -> "cancelled": the object was
                              deregistered (split, merged, demolished)
  rights / encumbrances     — a right registered or ended, a mortgage or an
                              arrest added or lifted (numbers, dates and
                              kinds only: an anonymous lookup is never shown
                              who holds a right)
  category, purpose, permitted_use, land_category, area, title

Three buckets: added (in --new only), removed (in --old only), changed.

Refused unless --force, because each would report artefacts as changes:
  * a run whose sidecar says it was not `complete` — the queries it never
    answered would read as "removed";
  * two runs that asked different questions (a different input list);
  * two runs in different modes — a list line from the free address search
    carries none of the fields a full record does, so every one would read
    as changed;
  * a data file that does not match its sidecar's digest.

An address run whose answer hit the site's 100-line cap is a SAMPLE that
differs from call to call (measured: 94 of 100 in common between two calls),
so added/removed between two such runs are annotated, not trusted.
"""

import argparse
import json
import re
import sys
from typing import Dict, List, Optional, Tuple

TRACKED_FIELDS = ("price", "currency", "status", "category", "title", "area",
                  "cad_cost_determination_date", "cad_cost_registration_date",
                  "cancel_date", "purpose", "permitted_use", "land_category",
                  "rights", "encumbrances", "list_kind")


def _load(path: str) -> List[dict]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _by_sku(rows: List[dict]) -> Tuple[Dict[str, dict], int]:
    """Index by sku. Rows with no sku or a repeated one cannot be matched."""
    indexed, unmatchable, seen_twice = {}, 0, set()
    for r in rows:
        sku = r.get("sku")
        if not sku:
            unmatchable += 1
        elif sku in indexed:
            seen_twice.add(sku)
        else:
            indexed[sku] = r
    for sku in seen_twice:
        indexed.pop(sku, None)
    return indexed, unmatchable + len(seen_twice)


def diff_rows(old: List[dict], new: List[dict]) -> dict:
    old_by, old_bad = _by_sku(old)
    new_by, new_bad = _by_sku(new)
    changed = []
    for sku in sorted(old_by.keys() & new_by.keys()):
        a, b = old_by[sku], new_by[sku]
        if a.get("detail_level") != b.get("detail_level"):
            # A list line and a full record of the same object: nothing a
            # list line lacks is a change.
            continue
        delta = {f: {"old": a.get(f), "new": b.get(f)}
                 for f in TRACKED_FIELDS if a.get(f) != b.get(f)}
        if delta:
            changed.append({"sku": sku, "title": b.get("title"), "changes": delta})
    return {
        "added": [new_by[s] for s in sorted(new_by.keys() - old_by.keys())],
        "removed": [old_by[s] for s in sorted(old_by.keys() - new_by.keys())],
        "changed": changed,
        "unmatchable_old": old_bad,
        "unmatchable_new": new_bad,
    }


def _meta(path: str) -> Optional[dict]:
    meta_path = re.sub(r"\.json$", "", path) + ".meta.json"
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _digest_mismatch(path: str, meta: dict) -> Optional[str]:
    want = (meta.get("files") or {}).get("json") or {}
    if not want.get("sha256"):
        return None
    from output_writer import file_digest
    try:
        got = file_digest(path)
    except OSError as e:
        return f"cannot be read ({e})"
    if got["sha256"] != want["sha256"]:
        return (f"does not match its .meta.json (run {meta.get('run_id')}): the "
                f"data was rewritten after the sidecar, or they come from two runs")
    return None


def check_comparable(old_path: str, new_path: str) -> List[str]:
    """Why these two runs cannot be diffed as-is (empty list = they can)."""
    problems, modes, asked = [], [], []
    for label, path in (("--old", old_path), ("--new", new_path)):
        meta = _meta(path)
        if meta is None:
            # No sidecar: nothing says the run was complete, what it asked
            # or that this file is the one it wrote. Every run of this repo
            # writes one, so its absence is itself the problem.
            problems.append(f"{label} ({path}) has no .meta.json beside it, so whether it "
                            f"is complete, and what it asked, cannot be checked")
            continue
        modes.append(meta.get("mode"))
        asked.append(meta.get("queries_sha256"))
        bad = _digest_mismatch(path, meta)
        if bad:
            problems.append(f"{label} ({path}) {bad}")
        if meta.get("status") != "complete":
            failed = [f.get("index") for f in meta.get("queries_failed") or []]
            problems.append(f"{label} ({path}) was a {meta.get('status')!r} run "
                            f"({meta.get('stop_reason')}); unanswered queries: {failed}")
    if len(asked) == 2 and None not in asked and asked[0] != asked[1]:
        problems.append("the runs asked different questions (different --cad-number / "
                        "--input / --address), so an object in one input and not the "
                        "other would read as added or removed")
    if len(modes) == 2 and modes[0] != modes[1]:
        problems.append(f"the runs are different modes ({modes[0]} vs {modes[1]})")
    return problems


def caveats(old_path: str, new_path: str) -> List[str]:
    out = []
    for label, path in (("--old", old_path), ("--new", new_path)):
        meta = _meta(path) or {}
        if meta.get("address_capped"):
            out.append(f"{label} ({path}): address queries {meta['address_capped']} hit "
                       f"the site's 100-line cap — a sample that changes between calls")
    return out


def _print(result: dict) -> None:
    print(f"[+] {len(result['added'])} added, {len(result['removed'])} removed, "
          f"{len(result['changed'])} changed.")
    for r in result["added"]:
        print(f"  + {r.get('sku')}  {r.get('title')}")
    for r in result["removed"]:
        print(f"  - {r.get('sku')}  {r.get('title')}")
    for c in result["changed"]:
        deltas = ", ".join(f"{f}: {v['old']!r} -> {v['new']!r}" for f, v in c["changes"].items())
        print(f"  ~ {c['sku']}  {deltas}")
    bad = result["unmatchable_old"] + result["unmatchable_new"]
    if bad:
        print(f"[!] {bad} row(s) had no sku or a repeated one and were not matched.")


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Diff two rosreestr-scraper JSON outputs by cadastral number.")
    p.add_argument("--old", required=True)
    p.add_argument("--new", required=True)
    p.add_argument("--out", help="also write the full diff as JSON here")
    p.add_argument("--fail-on-change", action="store_true",
                   help="exit 1 when anything changed — for a cron job that alerts")
    p.add_argument("--force", action="store_true",
                   help="diff even a partial run, two different modes, or a "
                        "data file that does not match its sidecar")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    problems = check_comparable(args.old, args.new)
    if problems and not args.force:
        print("[!] Refusing to diff — added/removed would include artefacts:")
        for line in problems:
            print(f"      {line}")
        print("    Re-run the incomplete side, or pass --force.")
        return 2
    try:
        result = diff_rows(_load(args.old), _load(args.new))
    except (OSError, json.JSONDecodeError) as e:
        print(f"[!] Could not read an input file: {e}")
        return 2
    notes = caveats(args.old, args.new)
    result["assortment_comparable"] = not notes
    result["assortment_caveats"] = notes
    _print(result)
    for line in notes:
        print(f"[!] {line}")
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"[+] Full diff written to {args.out}")
    changes = [result["changed"]] + ([result["added"], result["removed"]] if not notes else [])
    return 1 if args.fail_on_change and any(changes) else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(1)
