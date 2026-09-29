"""
output_writer.py
-----------------
Shared row model + JSON/CSV writers + the run-status/exit-code mapping used by
every engine in this repo.

Family core, adapted to a LOOKUP site. The rest of the family scrapes
listings — page 1, 2, 3 of a category — and this file used to speak in pages.
lk.rosreestr.ru has no listing to walk: the caller brings the questions (a
list of cadastral numbers, or an address) and every answer costs its own
captcha. So the unit of work here is a QUERY, not a page, and the sidecar
says `queries_*` where a sibling says `pages_*`. That is a written-down
divergence, not drift: pretending a query is a page would make "page 7 was
not fetched" mean "the 7th cadastral number in your file was not looked up",
which nobody reading the sidecar would guess.

What did NOT change, because a consumer of the family relies on it:

  * the family prefix of the row — source, scraped_at, url, sku, title,
    price, currency, category, page, position — same names, same order;
  * the exit codes: 0 ok · 1 crash · 2 bad usage · 3 blocked · 4 nothing
    found · 5 the content was never obtained · 6 partial;
  * a run that finds nothing writes nothing (--allow-empty opts out), and a
    failed run writes no sidecar beside the previous good output.

How the prefix maps onto a real-estate object:

  sku       the cadastral number — the site's own id for the object
  title     the address exactly as the site renders it (readableAddress)
  price     the cadastral value ("Кадастровая стоимость (руб)")
  currency  "RUB" — the site's own card labels that figure "(руб)", which is
            the only reason the column is filled; a row with no cadastral
            value leaves it null rather than asserting a currency for nothing
  category  the object type in the site's own words ("Помещение"), decoded
            through the site's own OBJECT_TYPE_CODES dictionary
  page      which QUERY of this run produced the row (1-based, input order)
  position  the row's 1-based position in that query's answer

Deliberately absent: the cadastral engineer's name, phone and certificate
number (cadEngFIO / cadEngPhone / cadEngCertNumber). The site publishes them;
they identify a private person and no use of this dataset needs them.
"""

import csv
import hashlib
import json
import os
import tempfile
import uuid
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

SOURCE = "lk.rosreestr.ru"


@dataclass
class Record:
    # --- family prefix: same names, same order, across the whole family ----
    source: str = SOURCE
    scraped_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    # The object's own page on the site: the online service opened with
    # ?cadNumber=, which is how the site's own front end links to one object.
    url: str = ""
    sku: Optional[str] = None          # cadastral number
    title: Optional[str] = None        # address as the site renders it
    price: Optional[float] = None      # cadastral value
    currency: Optional[str] = None     # "RUB" only beside a cadastral value
    category: Optional[str] = None     # object type, the site's own words
    page: Optional[int] = None         # which query of the run (1-based)
    position: Optional[int] = None     # position within that query's answer

    # --- site-specific, at the end -----------------------------------------
    # The text the caller asked about, verbatim: a cadastral number or an
    # address. Two rows with one `query` came from one answer.
    query: Optional[str] = None
    # "full" — the complete record from POST /account-back/on (one captcha);
    # "list" — a line of the free address search, which carries only the
    # number, the address, the object kind and whether it is current.
    detail_level: Optional[str] = None
    object_type_code: Optional[str] = None     # "002001003000"
    # "actual" / "cancelled" — the site's "Актуально" / "Погашено".
    status: Optional[str] = None
    cad_quarter: Optional[str] = None
    area: Optional[float] = None
    area_unit: Optional[str] = None            # as the site writes it: "кв.м"
    # Every main characteristic the record carries, verbatim:
    # [{"type": "Площадь", "value": 479.8, "unit": "кв.м"}]. `area` is read
    # out of it when the characteristic is an area; a structure's may be a
    # length or a depth instead, and would otherwise be lost.
    main_characteristics: Optional[List[Dict[str, Any]]] = None
    region: Optional[str] = None
    reg_date: Optional[str] = None             # date the number was assigned
    cancel_date: Optional[str] = None
    cad_cost_determination_date: Optional[str] = None
    cad_cost_registration_date: Optional[str] = None
    info_update_date: Optional[str] = None
    land_category: Optional[str] = None        # decoded, land plots only
    permitted_use: Optional[str] = None        # as the title document states it
    purpose: Optional[str] = None              # decoded ("Нежилое", "Жилой дом")
    floors: Optional[str] = None
    underground_floors: Optional[str] = None
    level_floor: Optional[str] = None          # the floor a room is on
    wall_material: Optional[str] = None
    year_built: Optional[str] = None
    year_commissioned: Optional[str] = None
    ownership_type: Optional[str] = None
    parent_cad_number: Optional[str] = None
    child_cad_numbers: Optional[List[str]] = None
    # [{"type": "Инвентарный номер", "number": "III"}]
    old_numbers: Optional[List[Dict[str, Any]]] = None
    # [{"number", "date", "type"}] — registered rights, as the card lists them.
    # An anonymous lookup is shown the right's number, date and kind, never
    # who holds it; this column carries exactly that and nothing more.
    rights: Optional[List[Dict[str, Any]]] = None
    encumbrances: Optional[List[Dict[str, Any]]] = None
    # List lines only: the address search's own kind label, verbatim —
    # "OKS" (a capital construction object), "FLAT" (a room), "PARCEL" (land).
    list_kind: Optional[str] = None
    # Full records only: the site's own status CODE, verbatim. `status` maps
    # it the way the site's card does ("1" is actual, anything else
    # cancelled); the raw value is kept so a code nobody has seen is not
    # silently folded into "cancelled" (third-party audit, 2026-09-29).
    status_code: Optional[str] = None


def _atomic_write(path: str, write: Callable) -> None:
    """Write `path` through a temporary file in the same directory, then rename.

    A crash or a full disk mid-write would otherwise leave a half-written
    file that no consumer can tell from a short run. os.replace is atomic
    within a filesystem.
    """
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-",
                               suffix=f"-{os.path.basename(path)}")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            write(f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# A leading one of these makes a spreadsheet treat the cell as a formula.
_FORMULA_LEAD = ("=", "+", "-", "@", "\t", "\r")


def _cell(value):
    """A value as it goes into a CSV cell: a list/dict as JSON text, else itself."""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return value


def csv_safe(value):
    """Neutralise a spreadsheet formula in a scraped string.

    Addresses and permitted-use texts are free text typed by registrars; the
    CSV is exactly what people open in Excel. Only `str` values are touched,
    so numbers keep their type.
    """
    if isinstance(value, str) and value[:1] in _FORMULA_LEAD:
        return "'" + value
    return value


def write_json(records: List[Record], path: str) -> None:
    _atomic_write(path, lambda f: json.dump(
        [asdict(r) for r in records], f, ensure_ascii=False, indent=2))


def write_csv(records: List[Record], path: str) -> None:
    # An empty result still gets the header row, so a consumer reads a table
    # with no rows instead of failing on a zero-byte file.
    fieldnames = list(asdict(Record()).keys())

    def _write(f):
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in records:
            writer.writerow({k: csv_safe(_cell(v)) for k, v in asdict(r).items()})

    _atomic_write(path, _write)


EXIT_OK = 0
EXIT_CRASH = 1
EXIT_USAGE = 2
# Something stood between the run and the answers: a refusal, a challenge
# nobody could solve, a captcha that kept being rejected.
EXIT_BLOCKED = 3
# Every query was answered, and the answer was "no such object".
EXIT_NO_PRODUCTS = 4
# Nothing was obtained at all: a navigation timeout, a dead proxy, a remote
# API error. Distinct from 4 because 4 is a statement about the REGISTRY and
# this run never reached it. Only for a run holding nothing.
EXIT_FETCH_FAILED = 5
EXIT_API_ERROR = EXIT_FETCH_FAILED
# Some queries were answered, then the run stopped or some failed. The output
# is written and is not a complete answer to the input.
EXIT_PARTIAL = 6

SCHEMA_VERSION = 1


@dataclass
class QueryOutcome:
    """What happened to one query, whatever engine ran it.

    `answered` means the site gave an answer we could read — including the
    answer "nothing matches", which is `records == []` with answered=True.
    Everything else is a failure, and `reason` says which.
    """
    index: int                          # 1-based, input order
    query: str
    answered: bool = False
    records: List[Record] = field(default_factory=list)
    reason: Optional[str] = None        # why it was not answered
    blocked: bool = False               # the reason is a refusal / challenge
    # Image tasks 2Captcha ACCEPTED for this query (createTask succeeded).
    # A task it refused (zero balance, bad key) is not counted: nothing was
    # charged for it. The price is `captcha_cost`, as 2Captcha reports it.
    captcha_solves: int = 0
    captcha_cost: float = 0.0
    # Elements/lines in the site's answer that were not the contract's shape
    # (no cadNumber / cadnum): kept out of the rows, and counted here.
    malformed: int = 0
    captcha_rejected: int = 0           # of those, how many the site refused
    autosolved: bool = False            # the Scraping Browser solved it itself
    capped: bool = False                # the site returned its maximum: a sample
    # How many objects the answer held WHEN it was answered. Rows can move
    # afterwards (--details puts a full record in place of its list line),
    # and "not found" must be judged by the answer, not by what is left.
    found: Optional[int] = None
    # A lookup the run planned itself (--details), not one the caller typed.
    # Kept out of the input digest diff_runs.py compares, because which
    # objects get details depends on the site's unstable 100-line sample.
    derived: bool = False


def file_digest(path: str) -> dict:
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
            size += len(chunk)
    return {"file": os.path.basename(path), "bytes": size, "sha256": h.hexdigest()}


def write_run_meta(out_prefix: str, meta: dict) -> str:
    """Write `<out>.meta.json` beside the output, return its path."""
    path = f"{out_prefix}.meta.json"
    _atomic_write(path, lambda f: json.dump(meta, f, ensure_ascii=False, indent=2))
    print(f"[+] Wrote run metadata -> {path} (status={meta.get('status')})")
    return path


def run_meta(*, status: str, stop_reason: str, mode: str, engine: str,
             outcomes: List[QueryOutcome], records: int,
             queries_requested: int, duplicates_dropped: int,
             files: Optional[Dict[str, dict]] = None,
             address_results: Optional[int] = None,
             spec: Optional[dict] = None) -> dict:
    """The sidecar for a finished run.

    `status` is what a consumer branches on:
      complete — every query was answered (some possibly with "not found")
      partial  — some were answered, some were not
      failed   — none were

    `queries_failed` names the failures BY INPUT POSITION and says why, so a
    caller can re-run exactly those lines. `queries_not_found` is separate:
    "the registry has no such number" is an answer, not a failure.

    `captcha_solves` / `captcha_rejected` / `autosolved` are the bill and its
    quality, measured rather than estimated: one paid image solve per full
    record, plus one for every answer the site refused.
    """
    failed = [{"index": o.index, "query": o.query, "reason": o.reason,
               # A full record the run planned itself (--details), not a
               # line of the caller's input: re-running the input re-plans it.
               "derived": o.derived}
              for o in outcomes if not o.answered]
    asked = [o.query for o in outcomes if not o.derived]
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": str(uuid.uuid4()),
        "engine": engine,
        "mode": mode,
        # WHAT kind of question, beyond the input text: the service URL, the
        # mode, the --list-kind filter and the --details reach. diff_runs.py
        # refuses two runs whose specs differ — a PARCEL-only run and a
        # FLAT-only run of one address read as additions and removals
        # otherwise (third-party audit, 2026-09-29).
        "query_spec": spec,
        "files": files or {},
        "source": SOURCE,
        "status": status,
        "stop_reason": stop_reason,
        "queries_requested": queries_requested,
        # WHAT was asked, so diff_runs.py can refuse to compare two runs that
        # asked different questions — a number in one input and not the other
        # would otherwise read as an object that disappeared.
        "queries_sha256": hashlib.sha256("\n".join(asked).encode("utf-8")).hexdigest(),
        "queries": asked if len(asked) <= 1000 else None,
        "queries_attempted": len(outcomes),
        "queries_answered": sum(1 for o in outcomes if o.answered),
        "queries_not_found": [o.index for o in outcomes if o.answered and not (
            o.found if o.found is not None else len(o.records))],
        "queries_failed": failed,
        "records": records,
        "duplicates_dropped": duplicates_dropped,
        "address_results": address_results,
        # Address queries whose answer hit the site's 100-line cap: those
        # lines are a sample that differs between calls, not every object
        # at the address.
        "address_capped": [o.index for o in outcomes if o.capped],
        "captcha_solves": sum(o.captcha_solves for o in outcomes),
        # Summed from getTaskResult's own `cost` field for solved tasks — the
        # figure 2Captcha states, not an estimate made here.
        "captcha_cost": round(sum(o.captcha_cost for o in outcomes), 6),
        "malformed_rows": sum(o.malformed for o in outcomes),
        "captcha_rejected": sum(o.captcha_rejected for o in outcomes),
        "autosolved": sum(1 for o in outcomes if o.autosolved),
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }


def save(records: List[Record], out_prefix: str, fmt: str,
         allow_empty: bool = False) -> bool:
    """Write JSON/CSV. Returns True if anything was written.

    On zero records nothing is written unless `allow_empty`: a run that
    failed and wrote `[]` would destroy the last good output, and a
    consumer cannot tell an empty answer from a failure.
    """
    if not records and not allow_empty:
        print(f"[!] 0 rows — refusing to write {out_prefix}.json/.csv, so an "
              f"earlier good result isn't overwritten with an empty one. "
              f"Pass --allow-empty if an empty result is the expected answer.")
        return False
    if fmt in ("json", "both"):
        write_json(records, f"{out_prefix}.json")
        print(f"[+] Saved {len(records)} rows -> {out_prefix}.json")
    if fmt in ("csv", "both"):
        write_csv(records, f"{out_prefix}.csv")
        print(f"[+] Saved {len(records)} rows -> {out_prefix}.csv")
    return True


def finish_run(outcomes: List[QueryOutcome], out_prefix: str, fmt: str,
               allow_empty: bool, *, mode: str, engine: str,
               queries_requested: int, stop_reason: str = "completed",
               address_results: Optional[int] = None,
               spec: Optional[dict] = None) -> int:
    """Merge, write output + sidecar, return the exit code.

    Shared by every engine so the status/exit mapping cannot drift between
    them. Rows are merged in INPUT order, never arrival order.

    A run is complete only when every requested query was attempted AND
    answered. The first half is not redundant: an engine that stopped early
    (a blocked session, Ctrl-C) never attempted the tail of the input, and
    those queries appear in no outcome at all — they are counted here rather
    than trusted to the engine to report.
    """
    outcomes = sorted(outcomes, key=lambda o: o.index)
    merged: List[Record] = []
    at: Dict[str, int] = {}
    before = 0
    for o in outcomes:
        for r in o.records:
            before += 1
            if r.sku is None:
                merged.append(r)
                continue
            if r.sku not in at:
                at[r.sku] = len(merged)
                merged.append(r)
            elif merged[at[r.sku]].detail_level == "list" and r.detail_level == "full":
                # The same object seen twice: the FULL record wins, in the
                # place the object first appeared. Keeping the first row
                # threw away a paid record for a list line when two
                # addresses overlapped (third-party audit, 2026-09-29).
                merged[at[r.sku]] = r
    duplicates = before - len(merged)

    answered = [o for o in outcomes if o.answered]
    unattempted = queries_requested - len(outcomes)
    complete = bool(outcomes) and len(answered) == len(outcomes) and unattempted <= 0
    blocked = any(o.blocked for o in outcomes if not o.answered)
    if unattempted > 0:
        print(f"[!] {unattempted} of {queries_requested} quer(y/ies) were never "
              f"attempted ({stop_reason}).")
        if stop_reason == "completed":
            stop_reason = "queries_unattempted"

    may_write_empty = allow_empty and complete
    wrote = save(merged, out_prefix, fmt, allow_empty=may_write_empty)
    if wrote:
        status = "complete" if complete else ("partial" if answered else "failed")
        write_run_meta(out_prefix, run_meta(
            status=status, stop_reason=stop_reason, mode=mode, engine=engine,
            outcomes=outcomes, records=len(merged), queries_requested=queries_requested,
            duplicates_dropped=duplicates, address_results=address_results, spec=spec,
            files={ext: file_digest(f"{out_prefix}.{ext}")
                   for ext in ("json", "csv") if fmt in (ext, "both")}))

    for o in outcomes:
        if not o.answered:
            print(f"[!] query {o.index} ({o.query!r}) was not answered: {o.reason}")

    if not merged:
        if answered and complete:
            # Every query answered, every answer "nothing". A fact about
            # the registry.
            print("[i] Every query was answered and nothing matched.")
            return EXIT_OK if may_write_empty else EXIT_NO_PRODUCTS
        # Some (or all) queries never got an answer and no row was gathered.
        # Not 6: a 6 promises an output file, and a run holding nothing
        # writes none — a consumer reading the file would find the PREVIOUS
        # run's data. The answered "nothing matched" ones are in the log.
        return EXIT_BLOCKED if blocked else EXIT_FETCH_FAILED
    if not complete:
        print(f"[!] Partial run: {len(answered)} of {queries_requested} "
              f"quer(y/ies) answered. See {out_prefix}.meta.json for which "
              f"failed and why.")
        return EXIT_PARTIAL
    return EXIT_OK
