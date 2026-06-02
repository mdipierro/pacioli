#!/usr/bin/env python
"""
A single page web app to edit a pacioli/ledger input file.
License: BSD <http://opensource.org/licenses/BSD-3-Clause>

It uses only the Python standard library (http.server) so it has no extra
dependencies.  The page is a textarea bound to the ledger file with live
validation and a trial-balance preview.

    python -m pacioli_erp.editor demo.ledger --port 8080

Endpoints
    GET  /                -> the single page editor (HTML + JS)
    GET  /api/load        -> {"text": "..."}                 the current file
    POST /api/validate    -> {"ok": bool, "error": str, "balance": [...]}  no write
    POST /api/save        -> same as validate, but writes the file on success
"""

import argparse
import io
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .ledger import Pacioli

PAGE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Pacioli editor &mdash; %(filename)s</title>
<style>
  /* shares the design tokens used by apps/pacioli/static/css/erp.css */
  :root {
    --erp-accent: #2d8cf0; --erp-muted: #7a8699; --erp-line: #e6e9ef;
    --erp-soft: #f7f9fc; --erp-neg: #c0392b; --erp-radius: 8px; --erp-fg: #1f2d3d;
  }
  * { box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
         margin: 0; color: var(--erp-fg); }
  header { background: #1f2d3d; color: #fff; padding: 10px 16px; display: flex; align-items: center; gap: 12px; }
  header h1 { font-size: 16px; margin: 0; font-weight: 600; }
  header .file { opacity: .7; font-size: 13px; }
  header .spacer { flex: 1; }
  button { background: var(--erp-accent); color: #fff; border: 0; padding: 8px 14px;
           border-radius: var(--erp-radius); cursor: pointer; font: inherit; font-size: 13px; }
  button.secondary { background: #555f6d; }
  button:disabled { opacity: .5; cursor: default; }
  #status { font-size: 13px; padding: 2px 8px; border-radius: var(--erp-radius); }
  .ok { background: #1aae6f33; color: #0a7a4a; }
  .err { background: #ed404033; color: #b3261e; }
  main { display: flex; height: calc(100vh - 49px); }
  #editor { flex: 1.4; border: 0; padding: 14px; font-family: ui-monospace, Menlo, Consolas, monospace;
            font-size: 13px; line-height: 1.5; resize: none; outline: none; tab-size: 4; color: var(--erp-fg); }
  #side { flex: 1; border-left: 1px solid var(--erp-line); overflow: auto; padding: 14px; background: var(--erp-soft); }
  #side h2 { font-size: .72rem; text-transform: uppercase; letter-spacing: .06em; color: var(--erp-muted); margin: 0 0 8px; }
  table { border-collapse: collapse; width: 100%%; font-size: 13px; font-variant-numeric: tabular-nums; }
  td { padding: 3px 6px; border-bottom: 1px solid var(--erp-line); }
  td.v { text-align: right; white-space: nowrap; }
  .neg { color: var(--erp-neg); }
  .lvl0 { font-weight: 600; }
  .acct { white-space: nowrap; }
  pre.error { color: var(--erp-neg); white-space: pre-wrap; font-size: 13px; }
</style>
</head>
<body>
<header>
  <h1>Pacioli</h1>
  <span class="file">%(filename)s</span>
  <span class="spacer"></span>
  <span id="status"></span>
  <button class="secondary" id="validate">Validate</button>
  <button id="save">Save</button>
</header>
<main>
  <textarea id="editor" spellcheck="false"></textarea>
  <div id="side"><h2>Trial balance</h2><div id="balance"></div></div>
</main>
<script>
const editor = document.getElementById('editor');
const statusEl = document.getElementById('status');
const balanceEl = document.getElementById('balance');
let timer = null;

function setStatus(ok, msg) {
  statusEl.textContent = msg;
  statusEl.className = ok ? 'ok' : 'err';
}

function fmt(v) {
  const n = parseFloat(v);
  const s = Math.abs(n).toFixed(2);
  return n < 0 ? '(' + s + ')' : s;
}

function renderBalance(data) {
  if (!data.ok) {
    balanceEl.innerHTML = '<pre class="error">' +
      data.error.replace(/&/g,'&amp;').replace(/</g,'&lt;') + '</pre>';
    setStatus(false, 'invalid');
    return;
  }
  setStatus(true, data.dirty ? 'valid (unsaved)' : 'valid');
  let rows = '';
  for (const item of data.balance) {
    const lvl = (item.name.match(/:/g) || []).length;
    const short = item.name.split(':').pop();
    for (let i = 0; i < item.amounts.length; i++) {
      const a = item.amounts[i];
      const neg = parseFloat(a.value) < 0 ? ' neg' : '';
      rows += '<tr><td class="acct lvl' + lvl + '" style="padding-left:' +
        (6 + lvl * 16) + 'px">' + (i === 0 ? short : '') + '</td>' +
        '<td class="v' + neg + '">' + fmt(a.value) + '</td>' +
        '<td>' + (a.commodity || '') + '</td></tr>';
    }
  }
  balanceEl.innerHTML = '<table>' + rows + '</table>';
}

async function call(endpoint, dirty) {
  const r = await fetch(endpoint, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({text: editor.value})
  });
  const data = await r.json();
  data.dirty = dirty;
  renderBalance(data);
  return data;
}

function scheduleValidate() {
  setStatus(false, 'editing…');
  clearTimeout(timer);
  timer = setTimeout(() => call('/api/validate', true), 500);
}

editor.addEventListener('input', scheduleValidate);
document.getElementById('validate').addEventListener('click', () => call('/api/validate', true));
document.getElementById('save').addEventListener('click', async () => {
  const data = await call('/api/save', false);
  if (data.ok) setStatus(true, 'saved');
});

(async function init() {
  const r = await fetch('/api/load');
  const data = await r.json();
  editor.value = data.text;
  call('/api/validate', false);
})();
</script>
</body>
</html>
"""


def analyze(text):
    """Parse and run a ledger given as text; return a JSON-able result dict."""
    p = Pacioli()
    try:
        p.load(io.StringIO(text))
        p.run()
    except Exception as ex:
        return {"ok": False, "error": "%s: %s" % (type(ex).__name__, ex), "balance": []}
    balance = []
    for name in sorted(p.accounts):
        wallet = p.accounts[name].wallet
        amounts = [
            {"commodity": commodity, "value": str(value)}
            for commodity, value in wallet
            if value != 0
        ]
        if amounts:
            balance.append({"name": name, "amounts": amounts})
    return {"ok": True, "error": "", "balance": balance}


class Handler(BaseHTTPRequestHandler):
    filename = "ledger"

    def log_message(self, *args):  # quieter server
        pass

    def _send(self, code, body, content_type="application/json"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            return {}

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            page = PAGE % {"filename": os.path.basename(self.filename)}
            self._send(200, page, "text/html; charset=utf-8")
        elif self.path == "/api/load":
            text = ""
            if os.path.exists(self.filename):
                with open(self.filename) as f:
                    text = f.read()
            self._send(200, json.dumps({"text": text}))
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        data = self._read_json()
        text = data.get("text", "")
        if self.path == "/api/validate":
            self._send(200, json.dumps(analyze(text)))
        elif self.path == "/api/save":
            result = analyze(text)
            if result["ok"]:
                with open(self.filename, "w") as f:
                    f.write(text)
            self._send(200, json.dumps(result))
        else:
            self._send(404, json.dumps({"error": "not found"}))


def serve(filename, port=8080, host="127.0.0.1"):
    Handler.filename = filename
    server = ThreadingHTTPServer((host, port), Handler)
    print("Pacioli editor for %s on http://%s:%s/" % (filename, host, port))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "filename",
        nargs="?",
        default=os.path.join(os.path.dirname(__file__), "examples", "demo.ledger"),
    )
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    serve(args.filename, args.port, args.host)


if __name__ == "__main__":
    main()
