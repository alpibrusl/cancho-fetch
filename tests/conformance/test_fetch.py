"""fetch against loopback servers (docs/design.md). Two builds are made, copied away and run: one allowed to talk to a
loopback peer (`--allow-private`), one not. The committed generated/ is left as the first build."""

import json
import os
import pathlib
import shutil
import socket
import ssl
import subprocess as _plain  # openssl for the certificates only; every other child goes through procs
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import procs as subprocess

ROOT = pathlib.Path(__file__).resolve().parents[2]
PORT = 18080
DEAD = 18081  # baked, and nothing listens (unless a test starts a server there)
TLS_OK = 18443      # https://localhost, a certificate for localhost from the trusted authority
TLS_NAME = 18444    # a certificate from the trusted authority for another name
TLS_CA = 18445      # a certificate for localhost from an authority that is not trusted
TLS_TWO = 18446     # a second good https host, with its own credential
TMP = pathlib.Path(tempfile.mkdtemp(prefix="fetch-test-"))
ENTRIES = ["http://127.0.0.1:%d" % PORT, "http://127.0.0.1:%d" % DEAD, "https://localhost:%d" % TLS_OK,
           "https://localhost:%d" % TLS_NAME, "https://localhost:%d" % TLS_CA, "https://localhost:%d" % TLS_TWO]
ALLOWED = ["127.0.0.1:%d" % PORT, "127.0.0.1:%d" % DEAD, "localhost:%d" % TLS_OK, "localhost:%d" % TLS_NAME, "localhost:%d" % TLS_CA,
           "localhost:%d" % TLS_TWO]
HOSTS = [x for e in ENTRIES for x in ("--host", e)]
TOKEN_ONE = TMP / "token-one"   # the credential of TLS_OK: Authorization: Bearer
TOKEN_TWO = TMP / "token-two"   # the credential of TLS_TWO: X-Api-Key
SECRET_ONE = "tok-one-secret-7f3a"
SECRET_TWO = "tok-two-secret-91bd"
CREDENTIALS = ["--credential", "localhost:%d=%s" % (TLS_OK, TOKEN_ONE), "--credential", "localhost:%d=%s,X-Api-Key" % (TLS_TWO, TOKEN_TWO)]


def make_certificates(where):
    """Two authorities, three leaves; returns the roots file (the first authority only) and a context per leaf."""
    def run_openssl(*args):
        r = _plain.run(["openssl", *args], cwd=where, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError("openssl %s: %s" % (" ".join(args), r.stderr))

    for ca in ("ca1", "ca2"):
        run_openssl("req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes", "-keyout", ca + ".key",
                    "-out", ca + ".pem", "-days", "30", "-subj", "/CN=test authority " + ca,
                    "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign,cRLSign")
    for name, ca, dns in (("good", "ca1", "localhost"), ("name", "ca1", "wrong.example"), ("untrusted", "ca2", "localhost")):
        run_openssl("req", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes", "-keyout", name + ".key",
                    "-out", name + ".csr", "-subj", "/CN=" + dns)
        (where / (name + ".ext")).write_text("basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\n"
                                              "extendedKeyUsage=serverAuth\nsubjectAltName=DNS:%s\n" % dns)
        run_openssl("x509", "-req", "-in", name + ".csr", "-CA", ca + ".pem", "-CAkey", ca + ".key", "-CAcreateserial",
                    "-out", name + ".pem", "-days", "30", "-extfile", name + ".ext")
    contexts = {}
    for name in ("good", "name", "untrusted"):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(str(where / (name + ".pem")), str(where / (name + ".key")))
        contexts[name] = ctx
    return str(where / "ca1.pem"), contexts


ROOTS = None
CONTEXTS = None


def build(name, *extra):
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "fetch_build.py"), *HOSTS, "--roots", ROOTS, *CREDENTIALS, *extra],
                       cwd=ROOT, capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        raise RuntimeError("build failed:\n" + r.stdout + r.stderr)
    dest = TMP / name
    shutil.copy(ROOT / "build" / "fetch", dest)
    return str(dest)


OPEN = STRICT = None


def setUpModule():
    global OPEN, STRICT, ROOTS, CONTEXTS
    ROOTS, CONTEXTS = make_certificates(TMP)
    TOKEN_ONE.write_text(SECRET_ONE + "\n")  # a file ends with a newline; the token does not
    TOKEN_TWO.write_text(SECRET_TWO)
    STRICT = build("strict")
    OPEN = build("open", "--allow-private")


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


class Server:
    """Answers each connection with `reply(request_bytes) -> bytes` (or None for silence), and records the requests."""

    def __init__(self, reply, hold=0.0, port=PORT, context=None):
        self.context = context
        self.reply = reply
        self.hold = hold
        self.requests = []
        # `localhost` may resolve to ::1 first (a CI runner has IPv6, this sandbox does not): listen on both when we can.
        try:
            self.sock = socket.socket(socket.AF_INET6)
            self.sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.sock.bind(("::", port))
        except OSError:
            self.sock = socket.socket()
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.sock.bind(("127.0.0.1", port))
        self.sock.listen(8)
        self.stop = False
        self.thread = threading.Thread(target=self.loop, daemon=True)
        self.thread.start()

    def loop(self):
        self.sock.settimeout(0.2)
        while not self.stop:
            try:
                conn, _ = self.sock.accept()
            except (socket.timeout, OSError):
                continue
            threading.Thread(target=self.serve, args=(conn,), daemon=True).start()

    def serve(self, conn):
        conn.settimeout(5)
        data = b""
        try:
            if self.context is not None:
                conn = self.context.wrap_socket(conn, server_side=True)
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
            head, _, rest = data.partition(b"\r\n\r\n")
            for line in head.split(b"\r\n")[1:]:
                if line.lower().startswith(b"content-length:"):
                    want = int(line.split(b":")[1])
                    while len(rest) < want:
                        more = conn.recv(65536)
                        if not more:
                            break
                        rest += more
                    data = head + b"\r\n\r\n" + rest
            self.requests.append(data)
            out = self.reply(data)
            if self.hold:
                time.sleep(self.hold)
            if out is not None:
                conn.sendall(out)
            if self.context is not None:
                conn.unwrap()  # close_notify
        except OSError:
            pass
        finally:
            conn.close()

    def close(self):
        self.stop = True
        self.thread.join()
        self.sock.close()


def ok(body=b"hi there\n", headers=b"Content-Type: text/plain\r\n"):
    return b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n%s\r\n%s" % (len(body), headers, body)


def run(binary, *args, timeout=30, chosen=True, stdin=None):
    """Two hosts are baked, so --host is required; the tests that are not about that choose the first. `stdin` is the text on
    standard input (none: the test runner's own, which a test of a tool that reads it must not rely on, so it is empty)."""
    if chosen and "--host" not in args and "introspect" not in args:
        args = ("--host", "127.0.0.1:%d" % PORT, *args)
    r = subprocess.run([binary, *args], capture_output=True, text=True, timeout=timeout, input="" if stdin is None else stdin)
    return r.returncode, (json.loads(r.stdout) if r.stdout.startswith("{") else r.stdout), r.stderr


class Fetch(unittest.TestCase):
    server = None

    def serve(self, reply, **kw):
        self.server = Server(reply, **kw)
        self.addCleanup(self.server.close)
        return self.server

    def hop(self, location, code=302):
        return lambda req: b"HTTP/1.1 %d Moved\r\nLocation: %s\r\nContent-Length: 0\r\n\r\n" % (code, location.encode())

    def rule(self, doc):
        self.assertFalse(doc["ok"], doc)
        return doc["error"]["rule"]

    def test_get(self):
        s = self.serve(lambda req: ok())
        rc, doc, _ = run(OPEN, "get", "/a/b?c=1")
        self.assertEqual(rc, 0)
        d = doc["data"]
        self.assertEqual((d["status"], d["reason"], d["body"], d["body_bytes"], d["host"], d["peer"]),
                         (200, "OK", "hi there\n", 9, "127.0.0.1:%d" % PORT, "127.0.0.1:%d" % PORT))
        self.assertEqual(d["method"], "get")
        self.assertIn({"name": "Content-Type", "value": "text/plain"}, d["headers"])
        req = s.requests[0]
        self.assertTrue(req.startswith(b"GET /a/b?c=1 HTTP/1.1\r\n"), req)
        self.assertIn(b"Host: 127.0.0.1:%d\r\n" % PORT, req)
        self.assertIn(b"Accept-Encoding: identity\r\n", req)

    def test_head_has_no_body(self):
        s = self.serve(lambda req: b"HTTP/1.1 200 OK\r\nContent-Length: 9\r\n\r\n")
        rc, doc, _ = run(OPEN, "head", "/")
        self.assertEqual(rc, 0)
        self.assertEqual((doc["data"]["body"], doc["data"]["body_bytes"], doc["data"]["method"]), ("", 0, "head"))
        self.assertTrue(s.requests[0].startswith(b"HEAD / HTTP/1.1"))

    def test_chunked(self):
        self.serve(lambda req: b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n6\r\nhello \r\n5\r\nworld\r\n0\r\n\r\n")
        rc, doc, _ = run(OPEN, "get", "/")
        self.assertEqual((rc, doc["data"]["body"]), (0, "hello world"))

    def test_close_delimited(self):
        self.serve(lambda req: b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\nuntil close")
        rc, doc, _ = run(OPEN, "get", "/")
        self.assertEqual((rc, doc["data"]["body"]), (0, "until close"))

    def test_binary_body_is_base64(self):
        self.serve(lambda req: ok(b"\xff\xfe\x00"))
        rc, doc, _ = run(OPEN, "get", "/")
        self.assertEqual(doc["data"]["body"], {"b64": "//4A"})

    def test_text_format(self):
        self.serve(lambda req: ok(b"just the body\n"))
        r = subprocess.run([OPEN, "--host", "127.0.0.1:%d" % PORT, "--format", "text", "get", "/"], capture_output=True, text=True, timeout=30)
        self.assertEqual((r.returncode, r.stdout), (0, "just the body\n"))

    def test_redirect_is_not_followed(self):
        s = self.serve(lambda req: b"HTTP/1.1 302 Found\r\nLocation: http://elsewhere.example/x\r\nContent-Length: 0\r\n\r\n")
        rc, doc, _ = run(OPEN, "get", "/")
        self.assertEqual((rc, doc["data"]["status"]), (0, 302))
        self.assertIn({"name": "Location", "value": "http://elsewhere.example/x"}, doc["data"]["headers"])
        self.assertEqual(len(s.requests), 1)

    def test_error_status_is_data(self):
        self.serve(lambda req: b"HTTP/1.1 404 Not Found\r\nContent-Length: 4\r\n\r\nnope")
        rc, doc, _ = run(OPEN, "get", "/")
        self.assertEqual((rc, doc["ok"], doc["data"]["status"]), (0, True, 404))

    def test_max_bytes_each_framing(self):
        big = b"x" * 5000
        for reply in (ok(big),
                      b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n%x\r\n%s\r\n0\r\n\r\n" % (len(big), big),
                      b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n" + big):
            s = self.serve(lambda req, r=reply: r)
            rc, doc, _ = run(OPEN, "--max-bytes", "100", "get", "/")
            self.assertEqual((rc, self.rule(doc)), (8, "fetch.body-too-large"))
            s.close()
            time.sleep(0.05)

    def test_a_body_larger_than_the_first_read_buffers(self):
        """Found by the benchmark: a response whose head and a long body arrive together left more body bytes after the head
        than the 8 KiB read buffer holds, and the tool trapped (SIGILL). Every framing, with the whole response in one send."""
        big = b"abcdefghij" * 20000  # 200,000 bytes
        for name, reply in (("length", ok(big)),
                            ("chunked", b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n%x\r\n%s\r\n0\r\n\r\n" % (len(big), big)),
                            ("close", b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n" + big)):
            s = self.serve(lambda req, r=reply: r)
            rc, doc, _ = run(OPEN, "get", "/")
            self.assertEqual((name, rc, doc["data"]["body_bytes"]), (name, 0, 200000), doc)
            s.close()
            time.sleep(0.05)

    def test_exactly_max_bytes(self):
        self.serve(lambda req: ok(b"y" * 100))
        rc, doc, _ = run(OPEN, "--max-bytes", "100", "get", "/")
        self.assertEqual((rc, doc["data"]["body_bytes"]), (0, 100))

    def test_timeout(self):
        self.serve(lambda req: None, hold=3)
        t = time.time()
        rc, doc, _ = run(OPEN, "--timeout-ms", "400", "get", "/")
        self.assertEqual(self.rule(doc), "fetch.timeout")
        self.assertLess(time.time() - t, 2.0)

    def test_timeout_in_the_body(self):
        self.serve(lambda req: b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\npart", hold=3)
        t = time.time()
        rc, doc, _ = run(OPEN, "--timeout-ms", "400", "get", "/")
        self.assertIn(self.rule(doc), ("fetch.timeout", "fetch.response-truncated"))
        self.assertLess(time.time() - t, 2.5)

    def test_truncated_body(self):
        self.serve(lambda req: b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\npart")
        rc, doc, _ = run(OPEN, "get", "/")
        self.assertEqual(self.rule(doc), "fetch.response-truncated")

    def test_not_http(self):
        self.serve(lambda req: b"SSH-2.0-OpenSSH\r\n\r\n")
        rc, doc, _ = run(OPEN, "get", "/")
        self.assertEqual(self.rule(doc), "fetch.response-malformed")

    def test_closed_port(self):
        rc, doc, _ = run(OPEN, "--host", "127.0.0.1:%d" % DEAD, "get", "/")
        self.assertEqual(self.rule(doc), "fetch.connect-failed")
        self.assertEqual(doc["error"]["detail"]["errno"], 111)

    def test_two_hosts_need_a_choice(self):
        s = self.serve(lambda req: ok())
        rc, doc, _ = run(OPEN, "get", "/", chosen=False)
        self.assertEqual((rc, self.rule(doc)), (2, "fetch.host-required"))
        self.assertEqual(s.requests, [])

    def test_unlisted_host(self):
        s = self.serve(lambda req: ok())
        rc, doc, _ = run(OPEN, "--host", "example.com", "get", "/")
        self.assertEqual((rc, self.rule(doc)), (2, "fetch.host-not-allowed"))
        self.assertEqual(doc["error"]["detail"]["allowed"], ALLOWED)
        self.assertEqual(s.requests, [])

    def test_url_is_not_a_path(self):
        s = self.serve(lambda req: ok())
        for target in ("http://example.com/", "//example.com/", "https://x/y"):
            rc, doc, _ = run(OPEN, "get", target)
            self.assertEqual((rc, self.rule(doc)), (2, "fetch.url-not-a-path"), target)
        self.assertEqual(s.requests, [])

    def test_bad_path_and_method(self):
        s = self.serve(lambda req: ok())
        self.assertEqual(self.rule(run(OPEN, "get", "/a b")[1]), "fetch.path-malformed")
        self.assertEqual(self.rule(run(OPEN, "get", "relative")[1]), "fetch.path-malformed")
        self.assertEqual(self.rule(run(OPEN, "put", "/")[1]), "fetch.method")
        self.assertEqual(s.requests, [])

    def test_post(self):
        s = self.serve(lambda req: b"HTTP/1.1 201 Created\r\nContent-Length: 2\r\n\r\nok")
        rc, doc, _ = run(OPEN, "--body", '{"a":1}', "--content-type", "application/json", "post", "/items")
        self.assertEqual((rc, doc["data"]["status"], doc["data"]["method"], doc["data"]["body"]), (0, 201, "post", "ok"))
        req = s.requests[0]
        self.assertTrue(req.startswith(b"POST /items HTTP/1.1\r\n"), req)
        self.assertIn(b"Content-Length: 7\r\n", req)
        self.assertIn(b"Content-Type: application/json\r\n", req)
        self.assertTrue(req.endswith(b"\r\n\r\n" + b'{"a":1}'), req)

    def test_post_body_from_standard_input(self):
        s = self.serve(lambda req: b"HTTP/1.1 201 Created\r\nContent-Length: 2\r\n\r\nok")
        body = '{"name":"caf\u00e9","n":[1,2,3]}\n'
        rc, doc, _ = run(OPEN, "--body-stdin", "--content-type", "application/json", "post", "/items", stdin=body)
        self.assertEqual((rc, doc["data"]["status"], doc["data"]["method"]), (0, 201, "post"), doc)
        req = s.requests[0]
        self.assertIn(b"Content-Length: %d\r\n" % len(body.encode()), req)
        self.assertTrue(req.endswith(b"\r\n\r\n" + body.encode()), req)

    def test_standard_input_body_is_bounded_and_may_be_empty(self):
        s = self.serve(lambda req: ok())
        rc, doc, _ = run(OPEN, "--body-stdin", "--content-type", "text/plain", "post", "/", stdin="x" * 65536)
        self.assertEqual(rc, 0, doc)
        self.assertEqual(len(s.requests), 1)
        self.assertTrue(s.requests[0].endswith(b"x" * 65536))
        rc, doc, _ = run(OPEN, "--body-stdin", "--content-type", "text/plain", "post", "/", stdin="")
        self.assertEqual(rc, 0, doc)
        self.assertIn(b"Content-Length: 0\r\n", s.requests[1])
        rc, doc, _ = run(OPEN, "--body-stdin", "--content-type", "text/plain", "post", "/", stdin="x" * 65537)
        self.assertEqual((rc, self.rule(doc)), (2, "fetch.request-body-too-large"))
        self.assertEqual(len(s.requests), 2)

    def test_standard_input_body_rules(self):
        s = self.serve(lambda req: ok())
        self.assertEqual(self.rule(run(OPEN, "--body", "a", "--body-stdin", "--content-type", "text/plain", "post", "/", stdin="b")[1]), "args.conflict")
        self.assertEqual(self.rule(run(OPEN, "--body-stdin", "post", "/", stdin="b")[1]), "fetch.body-required")
        for m in ("get", "head"):
            self.assertEqual(self.rule(run(OPEN, "--body-stdin", "--content-type", "text/plain", m, "/", stdin="b")[1]), "fetch.body-not-allowed")
        self.assertEqual(s.requests, [])

    def test_post_is_one_request_with_the_whole_body(self):
        s = self.serve(lambda req: ok())
        body = "x" * 60000
        rc, doc, _ = run(OPEN, "--body", body, "--content-type", "text/plain", "post", "/")
        self.assertEqual(rc, 0, doc)
        self.assertEqual(len(s.requests), 1)
        self.assertTrue(s.requests[0].endswith(body.encode()))

    def test_post_needs_a_body_and_a_type(self):
        s = self.serve(lambda req: ok())
        self.assertEqual(self.rule(run(OPEN, "post", "/")[1]), "fetch.body-required")
        self.assertEqual(self.rule(run(OPEN, "--body", "x", "post", "/")[1]), "fetch.body-required")
        self.assertEqual(self.rule(run(OPEN, "--content-type", "text/plain", "post", "/")[1]), "fetch.body-required")
        self.assertEqual(s.requests, [])

    def test_get_and_head_send_no_body(self):
        s = self.serve(lambda req: ok())
        for m in ("get", "head"):
            self.assertEqual(self.rule(run(OPEN, "--body", "x", "--content-type", "text/plain", m, "/")[1]), "fetch.body-not-allowed")
        self.assertEqual(s.requests, [])

    def test_post_body_cap_and_content_type_injection(self):
        s = self.serve(lambda req: ok())
        big = "x" * 65537
        self.assertEqual(self.rule(run(OPEN, "--body", big, "--content-type", "text/plain", "post", "/")[1]), "fetch.request-body-too-large")
        self.assertEqual(self.rule(run(OPEN, "--body", "x", "--content-type", "a/b\r\nX-Evil: 1", "post", "/")[1]), "fetch.header-malformed")
        self.assertEqual(s.requests, [])

    # ---- redirects: followed only to a host the build was given, and checked before a socket is opened
    def test_follow_a_path_on_the_same_host(self):
        def reply(req):
            if req.startswith(b"GET /old"):
                return b"HTTP/1.1 301 Moved\r\nLocation: /new?x=1#frag\r\nContent-Length: 0\r\n\r\n"
            return ok(b"arrived")
        s = self.serve(reply)
        rc, doc, _ = run(OPEN, "--max-redirects", "2", "get", "/old")
        d = doc["data"]
        self.assertEqual((rc, d["status"], d["body"], d["path"]), (0, 200, "arrived", "/new?x=1"))
        self.assertEqual(d["redirects"], [{"status": 301, "from": "127.0.0.1:%d" % PORT, "path": "/old", "to": "127.0.0.1:%d" % PORT, "to_path": "/new?x=1", "method": "get"}])
        self.assertEqual(len(s.requests), 2)

    def test_no_redirects_by_default_and_the_field_is_present(self):
        s = self.serve(self.hop("/elsewhere"))
        rc, doc, _ = run(OPEN, "get", "/")
        self.assertEqual((doc["data"]["status"], doc["data"]["redirects"], len(s.requests)), (302, [], 1))

    def test_follow_to_the_other_baked_host(self):
        other = Server(lambda req: ok(b"from the other"), port=DEAD)
        self.addCleanup(other.close)
        self.serve(self.hop("http://127.0.0.1:%d/there" % DEAD, 307))
        rc, doc, _ = run(OPEN, "--max-redirects", "1", "get", "/")
        d = doc["data"]
        self.assertEqual((rc, d["body"], d["host"], d["path"]), (0, "from the other", "127.0.0.1:%d" % DEAD, "/there"))
        self.assertTrue(other.requests[0].startswith(b"GET /there HTTP/1.1"))
        self.assertIn(b"Host: 127.0.0.1:%d\r\n" % DEAD, other.requests[0])

    def test_redirects_to_places_not_given_are_refused_unsent(self):
        other = Server(lambda req: ok(b"must not be reached"), port=DEAD)
        self.addCleanup(other.close)
        for location in ("http://example.com/x", "https://127.0.0.1:%d/x" % PORT, "next", "//127.0.0.1:%d/x" % DEAD,
                         "http://127.0.0.1:%d@evil.example/" % PORT, "http://127.0.0.1:9/x", "ftp://x/"):
            s = self.serve(self.hop(location))
            rc, doc, _ = run(OPEN, "--max-redirects", "3", "get", "/")
            self.assertEqual(self.rule(doc), "fetch.redirect-not-allowed", location)
            self.assertEqual(doc["error"]["detail"]["location"], location)
            self.assertEqual(len(s.requests), 1, location)
            self.assertEqual(other.requests, [], location)
            s.close()
            time.sleep(0.05)

    def test_redirect_limit(self):
        s = self.serve(self.hop("/again"))
        rc, doc, _ = run(OPEN, "--max-redirects", "2", "get", "/")
        self.assertEqual((rc, self.rule(doc)), (8, "fetch.redirect-limit"))
        self.assertEqual(len(s.requests), 3)

    def test_302_turns_a_post_into_a_get_and_307_keeps_it(self):
        def reply(req):
            if req.startswith(b"POST /a"):
                return b"HTTP/1.1 302 Found\r\nLocation: /b\r\nContent-Length: 0\r\n\r\n"
            if req.startswith(b"POST /c"):
                return b"HTTP/1.1 307 Temporary\r\nLocation: /d\r\nContent-Length: 0\r\n\r\n"
            return ok(b"end")
        s = self.serve(reply)
        args = ("--max-redirects", "1", "--body", "payload", "--content-type", "text/plain", "post")
        rc, doc, _ = run(OPEN, *args, "/a")
        self.assertEqual((doc["data"]["method"], doc["data"]["redirects"][0]["method"]), ("get", "get"))  # the envelope describes the last request
        self.assertTrue(s.requests[1].startswith(b"GET /b HTTP/1.1"))
        self.assertNotIn(b"payload", s.requests[1])
        self.assertNotIn(b"Content-Type", s.requests[1])
        rc, doc, _ = run(OPEN, *args, "/c")
        self.assertEqual(doc["data"]["redirects"][0]["method"], "post")
        self.assertTrue(s.requests[3].startswith(b"POST /d HTTP/1.1"))
        self.assertTrue(s.requests[3].endswith(b"payload"))

    def test_the_deadline_covers_the_whole_chain(self):
        s = self.serve(self.hop("/slow"), hold=0.3)
        t = time.time()
        rc, doc, _ = run(OPEN, "--max-redirects", "5", "--timeout-ms", "700", "get", "/")
        self.assertIn(self.rule(doc), ("fetch.timeout", "fetch.redirect-limit"))
        self.assertLess(time.time() - t, 3.0)

    def test_max_redirects_is_bounded(self):
        rc, doc, _ = run(OPEN, "--max-redirects", "6", "get", "/")
        self.assertEqual(doc["error"]["rule"], "args.bad-value")

    # ---- https: the pure TLS client of packages/tls, a trust store baked in at build time
    def tls_server(self, context, port, reply=None, **kw):
        s = Server(reply or (lambda req: ok(b"over tls\n")), port=port, context=CONTEXTS[context], **kw)
        self.addCleanup(s.close)
        return s

    def test_https_get(self):
        s = self.tls_server("good", TLS_OK)
        rc, doc, _ = run(OPEN, "--host", "localhost:%d" % TLS_OK, "get", "/secure?x=1")
        self.assertEqual(rc, 0, doc)
        d = doc["data"]
        self.assertEqual((d["scheme"], d["status"], d["body"], d["host"]), ("https", 200, "over tls\n", "localhost:%d" % TLS_OK))
        self.assertTrue(s.requests[0].startswith(b"GET /secure?x=1 HTTP/1.1\r\n"), s.requests)
        self.assertIn(b"Host: localhost:%d\r\n" % TLS_OK, s.requests[0])

    def test_https_post_and_chunked(self):
        def reply(req):
            if req.startswith(b"POST"):
                return b"HTTP/1.1 201 Created\r\nContent-Length: 2\r\n\r\nok"
            return b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n6\r\nhello \r\n5\r\nworld\r\n0\r\n\r\n"
        s = self.tls_server("good", TLS_OK, reply)
        h = ("--host", "localhost:%d" % TLS_OK)
        rc, doc, _ = run(OPEN, *h, "--body", "secret payload", "--content-type", "text/plain", "post", "/p")
        self.assertEqual((doc["data"]["status"], doc["data"]["method"]), (201, "post"))
        self.assertTrue(s.requests[0].endswith(b"secret payload"))
        rc, doc, _ = run(OPEN, *h, "get", "/c")
        self.assertEqual(doc["data"]["body"], "hello world")

    def test_https_a_certificate_for_another_name_is_refused(self):
        s = self.tls_server("name", TLS_NAME)
        rc, doc, _ = run(OPEN, "--host", "localhost:%d" % TLS_NAME, "get", "/")
        self.assertEqual((rc, self.rule(doc)), (8, "fetch.tls"))
        self.assertTrue(doc["error"]["detail"]["tag"].startswith("x509") or "name" in doc["error"]["detail"]["tag"], doc)
        self.assertEqual([r for r in s.requests if r], [])

    def test_https_an_untrusted_authority_is_refused(self):
        s = self.tls_server("untrusted", TLS_CA)
        rc, doc, _ = run(OPEN, "--host", "localhost:%d" % TLS_CA, "get", "/")
        self.assertEqual(self.rule(doc), "fetch.tls")
        self.assertEqual([r for r in s.requests if r], [])

    def test_https_to_a_plain_http_server_is_refused(self):
        s = self.serve(lambda req: ok(), port=TLS_OK)
        rc, doc, _ = run(OPEN, "--host", "localhost:%d" % TLS_OK, "get", "/")
        self.assertEqual(self.rule(doc), "fetch.tls")

    def test_https_silent_server_is_cut_at_the_deadline(self):
        raw = Server(lambda req: None, hold=4, port=TLS_OK)
        self.addCleanup(raw.close)
        t = time.time()
        rc, doc, _ = run(OPEN, "--host", "localhost:%d" % TLS_OK, "--timeout-ms", "500", "get", "/")
        self.assertIn(self.rule(doc), ("fetch.timeout", "fetch.tls"))
        self.assertLess(time.time() - t, 3.0)

    def test_https_body_cap(self):
        self.tls_server("good", TLS_OK, lambda req: ok(b"y" * 5000))
        rc, doc, _ = run(OPEN, "--host", "localhost:%d" % TLS_OK, "--max-bytes", "100", "get", "/")
        self.assertEqual(self.rule(doc), "fetch.body-too-large")

    def test_a_redirect_from_http_to_a_baked_https_host(self):
        self.tls_server("good", TLS_OK)
        self.serve(self.hop("https://localhost:%d/landed" % TLS_OK))
        rc, doc, _ = run(OPEN, "--max-redirects", "1", "get", "/")
        d = doc["data"]
        self.assertEqual((rc, d["scheme"], d["body"], d["path"]), (0, "https", "over tls\n", "/landed"))

    def test_a_redirect_to_the_wrong_scheme_of_a_baked_host_is_refused(self):
        other = self.tls_server("good", TLS_OK)
        for location in ("http://localhost:%d/x" % TLS_OK, "https://127.0.0.1:%d/x" % PORT):
            s = self.serve(self.hop(location))
            rc, doc, _ = run(OPEN, "--max-redirects", "1", "get", "/")
            self.assertEqual(self.rule(doc), "fetch.redirect-not-allowed", location)
            self.assertEqual([r for r in other.requests if r], [], location)
            s.close()
            time.sleep(0.05)

    # ---- credentials: a file named at build time, for one https host, sent to that host and no other
    def sent(self, server):
        return [r for r in server.requests if r]

    def test_a_credential_goes_to_its_host_as_a_bearer_token_and_nowhere_else_in_the_output(self):
        s = self.tls_server("good", TLS_OK)
        r = subprocess.run([OPEN, "--host", "localhost:%d" % TLS_OK, "get", "/private"], capture_output=True, text=True, timeout=30)
        doc = json.loads(r.stdout)
        self.assertEqual((r.returncode, doc["data"]["credentialed"], doc["data"]["status"]), (0, True, 200))
        self.assertIn(b"\r\nAuthorization: Bearer %s\r\n" % SECRET_ONE.encode(), self.sent(s)[0])
        self.assertNotIn(SECRET_ONE, r.stdout + r.stderr)  # the caller never sees it

    def test_a_credential_can_be_another_header(self):
        s = self.tls_server("good", TLS_TWO)
        rc, doc, _ = run(OPEN, "--host", "localhost:%d" % TLS_TWO, "get", "/")
        self.assertEqual((rc, doc["data"]["credentialed"]), (0, True))
        req = self.sent(s)[0]
        self.assertIn(b"\r\nX-Api-Key: %s\r\n" % SECRET_TWO.encode(), req)
        self.assertNotIn(b"Authorization", req)

    def test_a_host_without_a_credential_is_sent_none(self):
        s = self.serve(lambda req: ok())
        rc, doc, _ = run(OPEN, "get", "/")
        self.assertEqual((doc["data"]["credentialed"], b"Authorization" in self.sent(s)[0], b"X-Api-Key" in self.sent(s)[0]), (False, False, False))

    def test_a_redirect_never_carries_the_credential_to_another_host(self):
        first = self.tls_server("good", TLS_OK, self.hop("http://127.0.0.1:%d/plain" % PORT))
        second = self.serve(lambda req: ok(b"landed"))
        rc, doc, _ = run(OPEN, "--host", "localhost:%d" % TLS_OK, "--max-redirects", "1", "get", "/")
        self.assertEqual((doc["ok"], doc["data"]["body"]), (True, "landed"), doc)
        self.assertIn(SECRET_ONE.encode(), self.sent(first)[0])
        self.assertNotIn(SECRET_ONE.encode(), self.sent(second)[0])
        self.assertNotIn(b"Authorization", self.sent(second)[0])

    def test_a_redirect_to_another_credentialed_host_uses_that_hosts_own(self):
        first = self.tls_server("good", TLS_OK, self.hop("https://localhost:%d/two" % TLS_TWO))
        second = self.tls_server("good", TLS_TWO)
        rc, doc, _ = run(OPEN, "--host", "localhost:%d" % TLS_OK, "--max-redirects", "1", "get", "/")
        self.assertEqual(doc["ok"], True, doc)
        self.assertIn(SECRET_TWO.encode(), self.sent(second)[0])
        self.assertNotIn(SECRET_ONE.encode(), self.sent(second)[0])
        self.assertNotIn(SECRET_TWO.encode(), self.sent(first)[0])

    def test_an_unreadable_or_malformed_credential_stops_before_any_connection(self):
        s = self.tls_server("good", TLS_OK)
        keep = TOKEN_ONE.read_text()
        self.addCleanup(TOKEN_ONE.write_text, keep)
        TOKEN_ONE.unlink()
        rc, doc, _ = run(OPEN, "--host", "localhost:%d" % TLS_OK, "get", "/")
        self.assertEqual((rc, self.rule(doc)), (8, "fetch.credential-unavailable"))
        self.assertEqual(doc["error"]["detail"]["path"], str(TOKEN_ONE))
        for bad in ("", "\n", "two words", "tab\there", "ctl\x01char", "caf\u00e9"):
            TOKEN_ONE.write_text(bad)
            rc, doc, _ = run(OPEN, "--host", "localhost:%d" % TLS_OK, "get", "/")
            self.assertEqual(self.rule(doc), "fetch.credential-unavailable", repr(bad))
        self.assertEqual(self.sent(s), [])
        self.assertNotIn(SECRET_ONE, json.dumps(doc))

    def test_the_secret_is_not_in_introspect_or_the_skill(self):
        for word in ("introspect", "skill"):
            r = subprocess.run([OPEN, word], capture_output=True, text=True, timeout=30)
            self.assertNotIn(SECRET_ONE, r.stdout)
            self.assertNotIn(SECRET_TWO, r.stdout)

    def test_the_generator_refuses_a_credential_it_cannot_keep_apart(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        import fetch_build
        entries = ["http://127.0.0.1:1", "https://a.example", "https://b.example:8443"]
        ok_spec = fetch_build.parse_credentials(entries, ["a.example=/run/secrets/a", "b.example:8443=/run/secrets/b,X-Key"])
        self.assertEqual(ok_spec, {1: ("/run/secrets/a", "Authorization"), 2: ("/run/secrets/b", "X-Key")})
        for spec in ("127.0.0.1:1=/run/secrets/a",          # a plain http host
                     "c.example=/run/secrets/a",            # no such host
                     "a.example=relative/path",             # not absolute
                     "a.example=/run/../etc/passwd",        # a dot-dot
                     "a.example=/run/secrets/a,Cookie",     # a header a credential cannot be
                     "a.example=/run/secrets/a,Bad Header", # not a header name
                     "a.example", "a.example=", "=/x"):
            with self.assertRaises(SystemExit, msg=spec):
                fetch_build.parse_credentials(entries, [spec])
        with self.assertRaises(SystemExit):
            fetch_build.parse_credentials(entries, ["a.example=/x", "a.example=/y"])

    def test_header_injection_is_refused(self):
        s = self.serve(lambda req: ok())
        rc, doc, _ = run(OPEN, "--accept", "text/plain\r\nX-Evil: 1", "get", "/")
        self.assertEqual(self.rule(doc), "fetch.header-malformed")
        self.assertEqual(s.requests, [])

    def test_limits_are_clamped_by_refusal(self):
        rc, doc, _ = run(OPEN, "--max-bytes", "999999999", "get", "/")
        self.assertEqual(doc["error"]["rule"], "args.bad-value")
        rc, doc, _ = run(OPEN, "--timeout-ms", "0", "get", "/")
        self.assertEqual(doc["error"]["rule"], "args.bad-value")

    def test_strict_build_refuses_a_loopback_peer_before_sending(self):
        s = self.serve(lambda req: ok())
        rc, doc, _ = run(STRICT, "get", "/")
        self.assertEqual(self.rule(doc), "fetch.peer-not-public")
        self.assertEqual(doc["error"]["detail"]["peer"], "127.0.0.1:%d" % PORT)
        self.assertEqual([r for r in s.requests if r], [])

    def test_introspect_flags_are_well_formed(self):
        """A `;` in a help text splits a row of the flag table: the flags then list garbage (found by the MCP generator)."""
        rc, doc, _ = run(STRICT, "introspect")
        names = [f["name"] for f in doc["flags"]]
        self.assertEqual(names, ["--host", "--max-bytes", "--timeout-ms", "--accept", "--body", "--body-stdin", "--content-type", "--max-redirects", "--format"])
        for f in doc["flags"]:
            self.assertIn(f["kind"].split(":")[0], ("text", "nat", "choice", "bool"), f)
            self.assertTrue(f["help"], f)

    def test_introspect_authority_is_the_baked_hosts(self):
        rc, doc, _ = run(STRICT, "introspect")
        rows = {(l["name"], l["argument"]) for l in doc["authority"]["labels"]}
        self.assertEqual({r[1] for r in rows if r[0] == "net_out"}, set(ALLOWED))
        self.assertEqual({r[1] for r in rows if r[0] == "fs_read"}, {"/dev/urandom", str(TOKEN_ONE), str(TOKEN_TWO)})  # the seed and the two declared credentials
        names = {r[0] for r in rows}
        self.assertFalse(names & {"ffi", "net_in", "file_write", "dir_write", "exec"}, names)
        self.assertTrue(doc["authority"]["bounded"])


class PeerRanges(unittest.TestCase):
    def test_address_ranges(self):
        """tests/peer_test.cho: the IPv4 and IPv6 ranges a build without --allow-private refuses (no IPv6 socket is
        needed, so this is where the IPv6 ranges are held)."""
        r = subprocess.run(["cancho", "test", "tests/peer_test.cho", "tools/fetch/fetchpeer.cho", "--std"],
                           cwd=ROOT, capture_output=True, text=True, timeout=300)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("4 passed; 0 failed", r.stdout)


if __name__ == "__main__":
    unittest.main()
