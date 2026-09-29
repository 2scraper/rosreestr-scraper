"""
rosreestr_api.py
----------------
Everything this repo knows about lk.rosreestr.ru, in one place. The engines
drive a browser; this module decides what to ask, how to read the answer and
what the answer means. It has no driver in it and no JavaScript, so all three
engines read the site identically (family rule: site knowledge lives in the
parser and a handful of named constants, nowhere else).

What the service is, measured 2026-09-29 over a Russian exit:

  page      https://lk.rosreestr.ru/eservices/real-estate-objects-online
            "Справочная информация по объектам недвижимости в режиме online".
            Opens WITHOUT a Gosuslugi (ESIA) login. From outside Russia every
            Rosreestr host timed out at the TCP connect — no refusal page, no
            status, just silence — so a Russian exit is required.

  captcha   GET  /account-back/captcha.png        a 5-character image,
                                                  Cyrillic or Latin + digits
            GET  /account-back/captcha/{text}     200 = accepted, 403 = wrong
            ONE-USE: re-sending a body whose captcha was already spent
            answered 406 {"error":"Wrong captcha"}; the page fetches a fresh
            image after every search. The page's own banner: one captcha
            "позволяет просмотреть информацию об одном объекте".

  lookup    POST /account-back/on
            {"filterType":"cadastral","cadNumbers":["77:01:0001044:3030"],
             "captcha":"<text>"}
            -> {"elements":[{...the full record...}],"count":1}
            -> {"elements":[],"count":0}   for a number that does not exist
            The full record comes back in the SEARCH response; opening the
            object card in the UI makes no request of its own.

  address   GET  /account-back/address/search?term=<text>&objType=all
            -> [{"cadnum","full_name","actual","type"}]   NO captcha.
            CAPPED AT 100, and the hundred are not stable: two calls for
            "Москва, ул. Тверская, д. 13" 25 s apart (2026-09-29, two
            engines) returned 100 lines each, only 94 in common, in a
            different order. So 100 lines means "at least 100 exist and this
            is a sample" — see ADDRESS_CAP.
            The front end sends a query here whenever it contains anything
            but digits and ':' (its own rule, read from its bundle).

  codes     GET  /account-back/dictionary/{NAME}?sortKey=code
            The object-type list IS the "all categories" of the brief: nine
            kinds, from "Земельный участок" to "Машино-место". A snapshot
            ships as rosreestr_codes.json; --mode dictionaries refreshes it.

Not implemented, and said so rather than implied: the page's other three
search types — by restriction-of-right number, by previously assigned number,
by right number. Their labels are in the bundle; the filterType VALUES they
send were not observed, and this repo does not guess a request body.
"""

import html as html_lib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote, urlencode

from output_writer import Record

HOST = "lk.rosreestr.ru"
BASE = f"https://{HOST}"
PAGE_URL = f"{BASE}/eservices/real-estate-objects-online"
API = f"{BASE}/account-back"
ON_URL = f"{API}/on"
CAPTCHA_IMAGE_URL = f"{API}/captcha.png"
ADDRESS_SEARCH_URL = f"{API}/address/search"
DICTIONARY_URL = f"{API}/dictionary"

# The dictionaries the page itself loads, in the order it loads them.
DICTIONARIES = ("OBJECT_TYPE_CODES", "LAND_CATEGORY_CODES",
                "LAND_PERMITTED_USAGE_CODES", "ROOM_PURPOSE_CODES",
                "BUILDING_PURPOSE_CODES")

# The page's own filterType for "Поиск по адресу и кадастровому номеру" —
# the only one observed in a request.
FILTER_CADASTRAL = "cadastral"

# The status field of a full record: "1" is "Актуально"; the card renders
# every other value as "Погашено" (read from the page's own bundle).
STATUS_ACTUAL = "1"

# DOM anchors the engines wait on and type into. Ids, not classes: the
# classes are the UI kit's (`rros-ui-lib-...`) and churn with it.
SEL_QUERY = "#query"
SEL_CAPTCHA_INPUT = "#captcha"
SEL_SEARCH_BUTTON = "#realestateobjects-search"
# The image's src is a blob: URL (the page fetches captcha.png with XHR and
# shows it through URL.createObjectURL), so nothing in src says "captcha".
# Measured markup, 2026-09-29:
#   <img alt="captcha" class="rros-ui-lib-captcha-content-img" src="blob:…">
SEL_CAPTCHA_IMAGE = "img[alt='captcha'], img[class*='captcha-content-img']"
# The refresh link has no id; its text is the stable part.
CAPTCHA_REFRESH_TEXT = "Обновить картинку"

_CODES_PATH = Path(__file__).resolve().parent / "rosreestr_codes.json"
_codes_cache: Optional[Dict[str, Dict[str, str]]] = None


def codes() -> Dict[str, Dict[str, str]]:
    """The code dictionaries snapshot: {NAME: {code: label}}."""
    global _codes_cache
    if _codes_cache is None:
        data = json.loads(_CODES_PATH.read_text(encoding="utf-8"))
        _codes_cache = {k: v for k, v in data.items() if not k.startswith("_")}
    return _codes_cache


def object_types() -> Dict[str, str]:
    """{code: name} for the nine object kinds — the brief's "categories"."""
    return dict(codes().get("OBJECT_TYPE_CODES", {}))


def object_type_code(value: str) -> Optional[str]:
    """Accept a code ("002001003000") or a name ("Помещение", any case)."""
    value = (value or "").strip()
    types = object_types()
    if value in types:
        return value
    for code, name in types.items():
        if name.lower() == value.lower():
            return code
    return None


def decode(dictionary: str, code: Optional[str]) -> Optional[str]:
    """A code's label, or the code itself when the snapshot lacks it.

    Falling back to the code rather than to None keeps the fact the site
    stated; a newer code than the snapshot is a reason to refresh it, not a
    reason to drop the value.
    """
    if code in (None, ""):
        return None
    return codes().get(dictionary, {}).get(str(code), str(code))


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------
# A cadastral number is four colon-separated groups of digits:
# district:area:quarter:object ("77:01:0001044:3030"). The group widths vary
# by region and by age of the number, so only the SHAPE is checked.
_CAD_NUMBER_RE = re.compile(r"^\d{1,2}:\d{1,2}:\d{1,7}:\d{1,}$")


def normalise_cad_number(text: str) -> str:
    """Strip whitespace — including the NBSP a copied number often carries."""
    return re.sub(r"[\s  ]+", "", text or "")


def is_cad_number(text: str) -> bool:
    return bool(_CAD_NUMBER_RE.match(normalise_cad_number(text)))


def goes_to_address_search(text: str) -> bool:
    """The front end's own routing rule: anything but digits and ':' is an
    address. Mirrored so a query typed into the page and one given to this
    tool are sent to the same endpoint."""
    return any(ch not in "0123456789:" for ch in (text or "").strip())


def on_request_body(cad_number: str, captcha: str) -> dict:
    """The exact body the page sends to POST /account-back/on."""
    return {"filterType": FILTER_CADASTRAL,
            "cadNumbers": [normalise_cad_number(cad_number)],
            "captcha": captcha}


def address_search_url(term: str) -> str:
    """GET /account-back/address/search, spelled as the page spells it.

    Always `objType=all`, as the page sends it. The parameter does nothing:
    measured 2026-09-29, the same address answered 100 mixed lines for
    objType=all, PARCEL, OKS, FLAT, 002001001000, 002001003000 and with no
    objType at all — the kind counts moved only as much as they move
    between two identical calls. A filter by kind is therefore applied
    client-side (--list-kind), and cannot get past the 100-line cap.
    """
    params = {"term": " ".join((term or "").split()), "objType": "all"}
    return f"{ADDRESS_SEARCH_URL}?{urlencode(params)}"


def captcha_check_url(text: str) -> str:
    return f"{API}/captcha/{quote(text, safe='')}"


def dictionary_url(name: str) -> str:
    return f"{DICTIONARY_URL}/{name}?sortKey=code"


def object_url(cad_number: str) -> str:
    """A link to the object: the service page with ?cadNumber=.

    The page's own bundle builds exactly this query parameter when it links
    to one object (`URLSearchParams.append("cadNumber", …)`). Opening it
    still needs a captcha for the full card.
    """
    return f"{PAGE_URL}?{urlencode({'cadNumber': cad_number})}"


# ---------------------------------------------------------------------------
# Reading an answer
# ---------------------------------------------------------------------------
# What an answer from POST /on means. Order matters: a refusal status is
# checked before the body, because a proxy or a WAF can put anything in it.
ANSWERED = "answered"            # a readable {"elements": ...} body
WRONG_CAPTCHA = "wrong_captcha"  # 406 {"error":"Wrong captcha"} — measured
REFUSED = "refused"              # 401/403/429 — not observed on /on, handled
SERVER_ERROR = "server_error"    # 5xx
UNREADABLE = "unreadable"        # 200 with a body that is not the contract

REFUSAL_STATUSES = (401, 403, 429)


def classify_on(status: Optional[int], body: Optional[str]) -> Tuple[str, Optional[dict]]:
    """(kind, parsed_json_or_None) for one POST /on response."""
    parsed = None
    try:
        parsed = json.loads(body) if body else None
    except (TypeError, ValueError):
        parsed = None
    if status == 406 or (isinstance(parsed, dict)
                         and str(parsed.get("error", "")).lower() == "wrong captcha"):
        return WRONG_CAPTCHA, parsed
    if status in REFUSAL_STATUSES:
        return REFUSED, parsed
    if status is not None and status >= 500:
        return SERVER_ERROR, parsed
    if isinstance(parsed, dict) and isinstance(parsed.get("elements"), list):
        return ANSWERED, parsed
    return UNREADABLE, parsed


def _date(ms) -> Optional[str]:
    """Epoch milliseconds -> ISO date.

    Every date measured so far is an exact UTC midnight (1363910400000 =
    2013-03-22T00:00:00Z), so the UTC calendar date is the date the site
    means. Read in Moscow time it would be the same date at 03:00 — but a
    value stored as a Moscow midnight would be 21:00 UTC the day before,
    and none was seen.
    """
    if ms in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).date().isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _float(value) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace(",", ".").replace(" ", ""))
    except ValueError:
        return None


def _text(value) -> Optional[str]:
    if value in (None, ""):
        return None
    return str(value)


def _area(el: dict) -> Tuple[Optional[float], Optional[str]]:
    """Area and its unit from `mainCharacters`, falling back to `area`.

    The unit is taken from the record ("кв.м"), never assumed: a linear
    structure's main characteristic can be a length.
    """
    for ch in el.get("mainCharacters") or []:
        if isinstance(ch, dict) and ch.get("description") == "Площадь":
            return _float(ch.get("value")), _text(ch.get("unitDescription"))
    return _float(el.get("area")), None


def _rights(items, number_key, date_key, type_key) -> Optional[List[dict]]:
    if not items:
        return None
    out = []
    for it in items:
        if isinstance(it, dict):
            out.append({"number": _text(it.get(number_key)),
                        "date": _date(it.get(date_key)),
                        "type": _text(it.get(type_key))})
    return out or None


def record_from_element(el: dict, *, query: str, index: int, position: int) -> Record:
    """One full record from POST /on into a Record.

    Deliberately NOT read: cadEngFIO, cadEngPhone, cadEngCertNumber — see
    output_writer's docstring.
    """
    cad = _text(el.get("cadNumber"))
    address = el.get("address") if isinstance(el.get("address"), dict) else {}
    type_code = _text(el.get("objType"))
    cost = _float(el.get("cadCost"))
    area, unit = _area(el)
    purpose_code = _text(el.get("purpose"))
    # Rooms and buildings use different purpose dictionaries; try the
    # object's own first, then the other.
    purpose = None
    if purpose_code:
        purpose = (codes().get("ROOM_PURPOSE_CODES", {}).get(purpose_code)
                   or codes().get("BUILDING_PURPOSE_CODES", {}).get(purpose_code)
                   or purpose_code)
    old = [{"type": _text(o.get("numType")), "number": _text(o.get("numValue"))}
           for o in el.get("oldNumbers") or [] if isinstance(o, dict)]
    children = [c for c in (el.get("childCadNumbers") or []) if c]
    mains = [{"type": _text(ch.get("description")), "value": _float(ch.get("value")),
              "unit": _text(ch.get("unitDescription"))}
             for ch in el.get("mainCharacters") or [] if isinstance(ch, dict)]
    status = _text(el.get("status"))
    return Record(
        url=object_url(cad) if cad else "",
        sku=cad,
        title=_text(address.get("readableAddress")) or _text(address.get("address")),
        price=cost,
        currency="RUB" if cost is not None else None,
        category=decode("OBJECT_TYPE_CODES", type_code),
        page=index,
        position=position,
        query=query,
        detail_level="full",
        object_type_code=type_code,
        status=None if status is None else ("actual" if status == STATUS_ACTUAL else "cancelled"),
        cad_quarter=_text(el.get("cadQuarter")),
        area=area,
        area_unit=unit,
        main_characteristics=mains or None,
        region=_text(address.get("region")),
        reg_date=_date(el.get("regDate")),
        cancel_date=_date(el.get("cancelDate")),
        cad_cost_determination_date=_date(el.get("cadCostDeterminationDate")),
        cad_cost_registration_date=_date(el.get("cadCostRegistrationDate")),
        info_update_date=_date(el.get("infoUpdateDate")),
        land_category=decode("LAND_CATEGORY_CODES", _text(el.get("landCategory"))),
        # The card shows permittedUseByDoc under "Вид разрешенного
        # использования"; permittedUse is a code the card does not render.
        permitted_use=_text(el.get("permittedUseByDoc"))
        or decode("LAND_PERMITTED_USAGE_CODES", _text(el.get("permittedUse"))),
        purpose=purpose,
        floors=_text(el.get("floor")),
        underground_floors=_text(el.get("undergroundFloor")),
        level_floor=_text(el.get("levelFloor")),
        wall_material=_text(el.get("oksWallMaterial")),
        year_built=_text(el.get("oksYearBuild")),
        year_commissioned=_text(el.get("oksCommisioningYear")),
        ownership_type=_text(el.get("ownershipType")),
        parent_cad_number=_text(el.get("parentCadNumber")),
        child_cad_numbers=children or None,
        old_numbers=old or None,
        rights=_rights(el.get("rights"), "rightNumber", "rightRegDate", "rightTypeDesc"),
        encumbrances=_rights(el.get("encumbrances"), "encumbranceNumber", "startDate", "typeDesc"),
    )


def parse_on(data: dict, *, query: str, index: int) -> List[Record]:
    """Every element of an answered POST /on body, as Records."""
    out = []
    for el in data.get("elements") or []:
        if isinstance(el, dict) and el.get("cadNumber"):
            out.append(record_from_element(el, query=query, index=index,
                                           position=len(out) + 1))
    return out


# The address search labels a line with one of these. Mapped to the site's
# own object-type names only where the correspondence is certain: PARCEL is a
# land plot. OKS (объект капитального строительства) covers buildings,
# structures and unfinished construction alike, and FLAT is what the search
# calls every room — neither is mapped, and `category` stays null on those
# lines rather than asserting a kind the site did not state.
ADDRESS_KIND_TO_TYPE = {"PARCEL": "002001001000"}

# The most lines the address search returns. An answer of exactly this many
# is a SAMPLE of a larger set that changes from call to call (measured, see
# the module docstring): complete as an answer, never exhaustive.
ADDRESS_CAP = 100


def parse_address_search(body: str, *, query: str, index: int) -> List[Record]:
    """The free address search's lines, as `detail_level="list"` Records."""
    data = json.loads(body) if body else []
    out = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict) or not item.get("cadnum"):
            continue
        type_code = ADDRESS_KIND_TO_TYPE.get(str(item.get("type")))
        actual = item.get("actual")
        out.append(Record(
            url=object_url(item["cadnum"]),
            sku=str(item["cadnum"]),
            title=_text(item.get("full_name")),
            category=decode("OBJECT_TYPE_CODES", type_code),
            page=index,
            position=len(out) + 1,
            query=query,
            detail_level="list",
            object_type_code=type_code,
            # The list says only whether the entry is current; "not_actual"
            # rather than "cancelled", because the list does not say why.
            status=None if actual is None else ("actual" if actual else "not_actual"),
            list_kind=_text(item.get("type")),
        ))
    return out


def parse_dictionary(body: str) -> Dict[str, str]:
    data = json.loads(body)
    return {str(e["code"]): e["value"] for e in data if isinstance(e, dict) and "code" in e}


# ---------------------------------------------------------------------------
# Recognising the page itself
# ---------------------------------------------------------------------------
_EXTENSION_SCRIPT_RE = re.compile(
    r"<script\b[^>]*\bsrc=[\"'](?:chrome|moz)-extension://[^>]*>\s*</script>",
    re.IGNORECASE)


def strip_extension_scripts(html: str) -> str:
    """Remove browser-extension <script> tags before a marker scan.

    Over the Scraping Browser the auto-solve extension injects sixteen
    hunter/interceptor scripts into every page (counted on this site's page,
    2026-09-29: geetest, keycaptcha, arkoselabs, recaptcha, amazon_waf,
    yandex, lemin, turnstile, captchafox, mt_captcha…). Their paths name
    every captcha vendor there is, so an unstripped scan finds all of them
    on a page that has none of them.
    """
    return _EXTENSION_SCRIPT_RE.sub("", html or "")


def is_service_page(html: str) -> bool:
    """Was THIS page served — the online service with its search form?

    Structural, not a text marker: the search button's id is on the page
    the site serves, and on no refusal page, Chromium error page or proxy
    login page. Asked this way, a page nobody anticipated reads as "not the
    service" instead of as content.
    """
    return 'id="realestateobjects-search"' in (html or "")


def has_image_captcha(html: str) -> bool:
    """This site's own captcha: the image from /account-back/captcha.png
    next to an input named `captcha`.

    On this service the captcha is ALWAYS there — it is the site's, not an
    interstitial — so this answers "which captcha is on the page", never
    "was the page blocked".
    """
    cleaned = strip_extension_scripts(html)
    return bool(re.search(r"<img\b[^>]*captcha[^>]*>", cleaned, re.I)
                and re.search(r"<input\b[^>]*name=[\"']captcha[\"']", cleaned, re.I))


# Installed by every engine BEFORE any page script runs (Playwright
# add_init_script, pyppeteer evaluateOnNewDocument, Selenium's CDP
# Page.addScriptToEvaluateOnNewDocument). It records the page's own
# /account-back/ traffic — the captcha check, the lookup, the image loads —
# into window.__rrNet, so all three engines read the SAME evidence the same
# way instead of three drivers' network APIs with three sets of quirks.
#
# A plain script, not a function: the three installers take it verbatim, so
# no driver's evaluate dialect is involved. It only listens (a `loadend`
# handler, a cloned fetch body) and never changes a request.
NETWORK_HOOK_JS = r"""
(function () {
  if (window.__rrNetInstalled) return;
  window.__rrNetInstalled = true;
  window.__rrNet = [];
  var seq = 0;
  var keep = function (u) { return typeof u === 'string' && u.indexOf('/account-back/') !== -1; };
  var push = function (e) {
    try { e.n = ++seq; window.__rrNet.push(e); if (window.__rrNet.length > 300) window.__rrNet.shift(); } catch (x) {}
  };
  var open = XMLHttpRequest.prototype.open, send = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (m, u) {
    this.__rr = {m: String(m), u: String(u)};
    return open.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function (b) {
    var r = this.__rr, xhr = this;
    if (r && keep(r.u)) {
      xhr.addEventListener('loadend', function () {
        var body = null;
        try {
          if (xhr.responseType === '' || xhr.responseType === 'text') body = xhr.responseText;
          else if (xhr.responseType === 'json') body = JSON.stringify(xhr.response);
        } catch (x) {}
        push({m: r.m, u: r.u, s: xhr.status, body: body,
              req: typeof b === 'string' ? b : null, t: Date.now()});
      });
    }
    return send.apply(this, arguments);
  };
  if (window.fetch) {
    var f = window.fetch;
    window.fetch = function (input, init) {
      var u = typeof input === 'string' ? input : (input && input.url) || '';
      var p = f.apply(this, arguments);
      if (keep(u)) {
        p.then(function (resp) {
          return resp.clone().text().then(function (t) {
            push({m: (init && init.method) || 'GET', u: u, s: resp.status, body: t,
                  req: init && typeof init.body === 'string' ? init.body : null, t: Date.now()});
          });
        }).catch(function () {});
      }
      return p;
    };
  }
})();
"""

# What an entry of window.__rrNet is, as the flow reads it.
def net_kind(entry: dict) -> Optional[str]:
    """"on" | "captcha_check" | "captcha_image" | "address" | "dictionary" | None."""
    u = str(entry.get("u") or "").split("?")[0]
    if u.endswith("/account-back/on"):
        return "on"
    if u.endswith("/captcha.png"):
        return "captcha_image"
    if "/account-back/captcha/" in u:
        return "captcha_check"
    if u.endswith("/address/search"):
        return "address"
    if "/account-back/dictionary/" in u:
        return "dictionary"
    return None


def page_title(html: str) -> Optional[str]:
    m = re.search(r"<title[^>]*>(.*?)</title>", html or "", re.I | re.S)
    return html_lib.unescape(m.group(1)).strip() if m else None
