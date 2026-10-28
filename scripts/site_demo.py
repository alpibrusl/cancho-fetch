#!/usr/bin/env python3
"""The landing page's session, run for real: build a `fetch` for one loopback host, serve a loopback API, run the commands the page
replays, and write site/demo.js (what they printed) and site/index.html (docs/landing/index.template.html filled in).

    python3 scripts/site_demo.py --build          # build fetch for the demo host, then write the page
    python3 scripts/site_demo.py --fetch PATH     # use this build (made for http://127.0.0.1:18090 with --allow-private)
    python3 scripts/site_demo.py --build --check  # change nothing; fail if the committed page is not what this prints

Nothing on the page is typed by hand except the template: the replayed output, the refusal, the authority label and the benchmark
figures (which must still be in docs/design.md, section 8.7) come from here.
"""
import argparse
import http.server
import json
import pathlib
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading

ROOT = pathlib.Path(__file__).resolve().parent.parent
HOST, PORT, OTHER, REDIRECT = "127.0.0.1", 18090, 18099, 18092
BAKED = "%s:%d" % (HOST, PORT)
KEEP = ("generated", "manifests", "schemas", "build")


class Api(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json", extra=()):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in extra:
            self.send_header(k, v)
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/v1/items":
            self.send(200, b'{"items":[{"id":1,"name":"widget"},{"id":2,"name":"gadget"}]}')
        elif self.path == "/old":
            self.send(302, b"", extra=[("Location", "http://%s:%d/v1/items" % (HOST, REDIRECT))])
        elif self.path == "/big":
            self.send(200, b"lorem ipsum dolor sit amet " * 4000, "text/plain")
        else:
            self.send(404, b'{"error":"not found"}')

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.send(201, b'{"created":' + body.strip() + b"}")


def snapshot():
    out = {}
    for d in KEEP:
        for p in sorted((ROOT / d).rglob("*")) if (ROOT / d).exists() else []:
            if p.is_file() and "deps" not in p.parts and "__pycache__" not in p.parts:
                out[p] = p.read_bytes()
    return out


def build_fetch(dest):
    """fetch_build.py writes generated/, manifests/ and build/ for the host list it is given; put back what was there."""
    before = snapshot()
    try:
        r = subprocess.run([sys.executable, str(ROOT / "scripts" / "fetch_build.py"), "--host", "http://%s" % BAKED, "--allow-private"],
                           cwd=ROOT, capture_output=True, text=True, timeout=1500)
        if r.returncode != 0:
            sys.exit("site_demo: the build failed:\n" + r.stdout[-1500:] + r.stderr[-1500:])
        shutil.copy(ROOT / "build" / "fetch", dest)
    finally:
        for p in snapshot():
            if p not in before:
                p.unlink()
        for p, data in before.items():
            if not p.exists() or p.read_bytes() != data:
                p.write_bytes(data)


def run(fetch, args, stdin=None):
    r = subprocess.run([fetch, *args], input=stdin, capture_output=True, timeout=60)
    return r.stdout.decode(), r.returncode


def compact(o):
    return json.dumps(o, separators=(",", ":"), ensure_ascii=False)


def session(fetch):
    """The replay, and the facts the page quotes: ([items], start state, the refusal, the authority label)."""
    out, rc = run(fetch, ["introspect"])
    label = [x for x in json.loads(out)["authority"]["labels"] if x["name"] == "net_out"]
    assert [x["argument"] for x in label] == [BAKED], label

    got, rc_ok = run(fetch, ["get", "/v1/items", "--host", BAKED])
    ok = json.loads(got)["data"]
    assert rc_ok == 0 and ok["status"] == 200
    refused, rc_host = run(fetch, ["get", "/v1/items", "--host", "%s:%d" % (HOST, OTHER)])
    host_err = json.loads(refused)["error"]
    assert rc_host == 2 and host_err["rule"] == "fetch.host-not-allowed" and host_err["detail"]["allowed"] == [BAKED]
    red, rc_red = run(fetch, ["get", "/old", "--host", BAKED, "--max-redirects", "3"])
    red_err = json.loads(red)["error"]
    assert rc_red == 2 and red_err["rule"] == "fetch.redirect-not-allowed", red_err
    big, rc_big = run(fetch, ["get", "/big", "--host", BAKED, "--max-bytes", "2000"])
    big_err = json.loads(big)["error"]
    assert rc_big == 8 and big_err["rule"] == "fetch.body-too-large"
    posted, rc_post = run(fetch, ["post", "/v1/items", "--host", BAKED, "--body-stdin", "--content-type", "application/json"], b'{"name":"sprocket"}\n')
    post = json.loads(posted)["data"]
    assert rc_post == 0 and post["status"] == 201

    def err(e, *detail):
        return compact({"rule": e["rule"], "hint": e["hint"], "detail": {k: e["detail"][k] for k in detail}})

    def state(nodes, stamps, verdict):
        return {"nodes": nodes, "stamps": stamps, "verdict": verdict}

    start = state({}, {}, ["fetch holds its hosts in the binary. Nothing here is a flag.", ""])
    items = [
        {"l": ["d", "$ fetch introspect | jq -c '.authority.labels[] | select(.name == \"net_out\")'"]},
        {"l": ["a", compact(label[0])], "s": state({}, {}, ["One host in the row: %s. That is all it can dial." % BAKED, ""])},
        {"l": ["d", "$ fetch get /v1/items --host %s" % BAKED]},
        {"l": ["g", compact({k: ok[k] for k in ("method", "scheme", "host", "path", "status", "body", "redirects")})],
         "s": state({"api": "ok"}, {"api": "200 OK"}, ["A baked host answers. The body is in the record.", "ok"])},
        {"l": ["d", "$ fetch get /v1/items --host %s:%d" % (HOST, OTHER)]},
        {"l": ["b", err(host_err, "host", "allowed")]},
        {"l": ["d", "# exit status 2"],
         "s": state({"other": "bad"}, {"other": "refused"}, ["A host outside the list is refused before a socket opens.", "ok"])},
        {"l": ["d", "$ fetch get /old --host %s --max-redirects 3" % BAKED]},
        {"l": ["b", err(red_err, "location")]},
        {"l": ["d", "# exit status 2"],
         "s": state({"api": "ok", "evil": "bad"}, {"api": "302", "evil": "not followed"}, ["The server redirected somewhere not in the list. Not followed.", "ok"])},
        {"l": ["d", "$ fetch get /big --host %s --max-bytes 2000" % BAKED]},
        {"l": ["b", err(big_err, "max_bytes")]},
        {"l": ["d", "# exit status 8"],
         "s": state({"api": "bad"}, {"api": "too large"}, ["The body is over the cap. It is refused, not cut off and passed on.", "ok"])},
        {"l": ["d", "$ echo '{\"name\":\"sprocket\"}' | fetch post /v1/items --host %s --body-stdin --content-type application/json" % BAKED]},
        {"l": ["g", compact({k: post[k] for k in ("method", "scheme", "host", "path", "status", "body")})],
         "s": state({"api": "ok"}, {"api": "201 Created"}, ["A POST goes only where a GET may. The same row covers both.", "ok"])},
    ]
    refusal = json.dumps({"ok": False, "command": "fetch", "error": {"code": host_err["code"], "rule": host_err["rule"], "message": host_err["message"],
                                                                 "hint": host_err["hint"], "repair": host_err["repair"], "detail": host_err["detail"]}},
                         indent=2, ensure_ascii=False)
    return items, start, refusal, label[0]


# What the page claims about speed: docs/design.md 8.7 (curl 8.5.0 with OpenSSL 3.0.13, one process per request, medians).
BARS = [("1 KiB, http", 2.0, 7.4), ("1 KiB, https", 3.6, 9.2), ("1 MiB, https, text", 5.0, 12.7), ("16 MB, https, json", 91.7, 21.1)]


def bars_html(design):
    for name, a, b in BARS:
        for v in (a, b):
            need = "%.1f ms" % v
            if need not in design:
                sys.exit("site_demo: docs/design.md 8.7 no longer says %r (%s); update BARS in scripts/site_demo.py" % (need, name))
    rows = []
    for name, f, c in BARS:
        top = max(f, c)
        rows.append('<div class="brow"><span>%s</span><div class="tr"><div class="bar1" style="--w:%.1f%%"><span>fetch %g ms</span></div>'
                    '<div class="bar1 inc" style="--w:%.1f%%"><span>curl %g ms</span></div></div></div>' % (name, f / top * 100, f, c / top * 100, c))
    return "<h3>Time for one request, one process each (shorter is faster)</h3>" + "".join(rows)


PROMPT = r"""Try cancho-fetch against a server on this machine and tell me what you see. Work in a new empty directory, never use sudo, and report honestly: do not retry a failing command in a loop and do not hide a failure.

1. Check that git, python3, cargo (Rust) and clang exist. If one is missing, stop and tell me which; do not install system packages yourself.
2. Get the sources and the compiler they pin:
   git clone https://github.com/alpibrusl/cancho
   git clone https://github.com/alpibrusl/cancho-fetch && cd cancho-fetch
   REV=$(sed -n 's/^cancho *= *"\([0-9a-f]*\)".*/\1/p' cancho.toml)
   (cd ../cancho && git fetch -q origin && git checkout "$REV" && cargo build --release -p cancho)
   export PATH=$PWD/../cancho/target/release:$PATH
3. In another shell, serve a directory with one file, items.json, on 127.0.0.1:18090:
   mkdir -p /tmp/api && echo '{"items":[1,2]}' > /tmp/api/items.json && python3 -m http.server 18090 --bind 127.0.0.1 --directory /tmp/api
4. Build a fetch for that one host (about three minutes: most of it is compiling the TLS packages):
   python3 scripts/fetch_build.py --host http://127.0.0.1:18090 --allow-private
5. Run, and note the exit status and `rule` (or `data.status`) of each:
   build/fetch get /items.json --host 127.0.0.1:18090            # expect status 200
   build/fetch get /items.json --host 127.0.0.1:18099            # expect fetch.host-not-allowed, exit 2
   build/fetch introspect | jq -c '.authority.labels[] | select(.name == "net_out")'   # the only host in the row
6. Tell me the three results, what the build printed if it failed, and anything that surprised you.
Docs: README.md, docs/design.md."""


def render(items, start, refusal, label, design):
    tpl = (ROOT / "docs" / "landing" / "index.template.html").read_text()
    import html
    page = (tpl.replace("{{REFUSAL}}", html.escape(refusal)).replace("{{PROMPT}}", html.escape(PROMPT)).replace("{{BARS}}", bars_html(design))
               .replace("{{LABEL}}", html.escape(compact(label))).replace("{{BAKED}}", html.escape(BAKED)))
    assert "{{" not in page
    demo = "window.FDEMO = " + json.dumps({"start": start, "items": items}, indent=1, ensure_ascii=False).replace("</", "<\\/") + ";\n"
    return page, demo


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--fetch", help="a build of fetch for http://%s with --allow-private" % BAKED)
    ap.add_argument("--build", action="store_true", help="build it (fetch_build.py), leaving generated/ and manifests/ as they were")
    ap.add_argument("--check", action="store_true", help="change nothing; fail when site/ is not what this prints")
    a = ap.parse_args()
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="site-demo-"))
    try:
        fetch = a.fetch
        if a.build or not fetch:
            fetch = str(tmp / "fetch")
            build_fetch(fetch)
        socketserver.ThreadingTCPServer.allow_reuse_address = True
        srv = socketserver.ThreadingTCPServer((HOST, PORT), Api)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            items, start, refusal, label = session(fetch)
        finally:
            srv.shutdown()
        page, demo = render(items, start, refusal, label, (ROOT / "docs" / "design.md").read_text())
        site = ROOT / "site"
        if a.check:
            stale = [n for n, text in (("index.html", page), ("demo.js", demo)) if not (site / n).exists() or (site / n).read_text() != text]
            if stale:
                sys.exit("site_demo: %s is not what the built fetch prints; run scripts/site_demo.py" % " and ".join("site/" + n for n in stale))
            print("site_demo: site/index.html and site/demo.js are what fetch prints")
        else:
            (site / "index.html").write_text(page)
            (site / "demo.js").write_text(demo)
            print("site_demo: wrote site/index.html and site/demo.js (%d replay lines)" % len(items))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
