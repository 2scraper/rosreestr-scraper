"""
tests/mock_site.py
------------------
A local stand-in for lk.rosreestr.ru, built from what was MEASURED on the real
one (2026-09-29), so every engine can be driven end to end with no network,
no key and no Russian exit:

  * the form uses the real page's ids — #query, #captcha,
    #realestateobjects-search — and the real captcha <img alt="captcha">
    shown through a blob: URL fetched with XHR, as the real page does;
  * GET  /account-back/captcha.png         a new image (and a new answer)
  * GET  /account-back/captcha/{text}      200 if right, 403 if wrong
  * POST /account-back/on                  the real response bodies from
                                           fixtures.json; 406 "Wrong captcha"
                                           for a wrong or already-spent answer
  * GET  /account-back/address/search      the real 100-line answer
  * the page fetches a NEW image after every search, and the search button
    is enabled only once the site's own check accepted the answer.

The image is a real PNG whose pixels are irrelevant: the "solver" used in the
end-to-end test asks this server for the answer (/_test/answer), which is the
one thing a real solver does by reading the picture.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

FIXTURES = json.loads((Path(__file__).resolve().parent.parent / "fixtures.json")
                      .read_text(encoding="utf-8"))
KNOWN = {"77:01:0001044:3030": FIXTURES["on_found"]["body"]}

# A 1x1 PNG. The engines screenshot the <img>, so its pixels never matter.
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082")

PAGE = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<title>Личный кабинет</title></head><body>
<div id="personal-cabinet-root">
 <img alt="captcha" class="rros-ui-lib-captcha-content-img" src="" width="200" height="50">
 <button type="button" id="refresh"><span>Обновить картинку</span></button>
 <input type="text" id="captcha" name="captcha" placeholder="Введите символы">
 <input type="text" id="query" name="query">
 <button id="realestateobjects-search" disabled>НАЙТИ</button>
 <div id="results"></div>
</div>
<script>
(function () {
  var img = document.querySelector('img[alt=captcha]');
  var cap = document.getElementById('captcha');
  var q = document.getElementById('query');
  var btn = document.getElementById('realestateobjects-search');
  var ok = false;
  function loadCaptcha() {
    var x = new XMLHttpRequest();
    x.open('GET', '/account-back/captcha.png'); x.responseType = 'blob';
    x.onload = function () { img.src = URL.createObjectURL(x.response); };
    x.send();
  }
  function update() { btn.disabled = !(ok && q.value); }
  cap.addEventListener('input', function () {
    ok = false; update();
    if (!cap.value) return;
    var x = new XMLHttpRequest();
    x.open('GET', '/account-back/captcha/' + encodeURIComponent(cap.value));
    x.onload = function () { ok = x.status === 200; update(); };
    x.send();
  });
  q.addEventListener('input', update);
  document.getElementById('refresh').addEventListener('click', loadCaptcha);
  btn.addEventListener('click', function () {
    var x = new XMLHttpRequest();
    x.open('POST', '/account-back/on');
    x.setRequestHeader('Content-Type', 'application/json');
    // The next image is fetched once the answer is in, as on the real page
    // (every live lookup answered 200 with the image still valid).
    x.onload = function () { document.getElementById('results').textContent = x.responseText; loadCaptcha(); };
    x.send(JSON.stringify({filterType: 'cadastral', cadNumbers: [q.value], captcha: cap.value}));
  });
  loadCaptcha();
})();
</script></body></html>"""


class MockSite:
    """Run with `with MockSite() as site:`; `site.url` is the service page."""

    def __init__(self, wrong_checks=0):
        self.answer = None
        self.spent = True
        self.counter = 0
        self.lookups = []
        self.wrong_checks = wrong_checks   # the first N checks answer 403 anyway
        self._lock = threading.Lock()

    def _new_answer(self):
        with self._lock:
            self.counter += 1
            # Cyrillic, like most of the real ones.
            self.answer = f"ду{self.counter:03d}"
            self.spent = False

    def __enter__(self):
        site = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # quiet
                pass

            def _send(self, code, body=b"", ctype="application/json"):
                if isinstance(body, str):
                    body = body.encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                path = urlparse(self.path).path
                if path == "/eservices/real-estate-objects-online":
                    return self._send(200, PAGE, "text/html; charset=utf-8")
                if path == "/account-back/captcha.png":
                    site._new_answer()
                    return self._send(200, PNG, "image/png")
                if path.startswith("/account-back/captcha/"):
                    text = unquote(path.rsplit("/", 1)[1])
                    good = text == site.answer and not site.spent
                    if good and site.wrong_checks > 0:
                        # Forced refusal of a COMPLETE right answer, and the
                        # image is replaced as the real page does on refresh.
                        site.wrong_checks -= 1
                        site.spent = True
                        return self._send(403, '{"error":"Wrong captcha"}')
                    return self._send(200 if good else 403, "" if good else '{"error":"Wrong captcha"}')
                if path == "/account-back/address/search":
                    return self._send(200, FIXTURES["address_search"]["body"])
                if path == "/_test/answer":
                    return self._send(200, site.answer or "", "text/plain; charset=utf-8")
                return self._send(404, '{"error":"not found"}')

            def do_POST(self):
                path = urlparse(self.path).path
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                if path != "/account-back/on":
                    return self._send(404, "{}")
                site.lookups.append(body)
                if body.get("captcha") != site.answer or site.spent:
                    return self._send(406, '{"error":"Wrong captcha"}')
                site.spent = True
                number = (body.get("cadNumbers") or [None])[0]
                return self._send(200, KNOWN.get(number, '{"elements":[],"count":0}'))

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.base = f"http://{host}:{port}"
        self.url = f"{self.base}/eservices/real-estate-objects-online"
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
