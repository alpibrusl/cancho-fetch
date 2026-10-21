"""fetch against loopback servers (docs/design.md). Two builds are made, copied away and run: one allowed to talk to a
loopback peer (`--allow-private`), one not. The committed generated/ is left as the first build."""

import json
import os
import pathlib
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import procs as subprocess

ROOT = pathlib.Path(__file__).resolve().parents[2]
PORT = 18080
DEAD = 18081  # baked, and nothing listens
TMP = pathlib.Path(tempfile.mkdtemp(prefix="fetch-test-"))
HOSTS = ["--host", "http://127.0.0.1:%d" % PORT, "--host", "http://127.0.0.1:%d" % DEAD]


def build(name, *extra):
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "fetch_build.py"), *HOSTS, *extra],
                       cwd=ROOT, capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        raise RuntimeError("build failed:\n" + r.stdout + r.stderr)
    dest = TMP / name
    shutil.copy(ROOT / "build" / "fetch", dest)
    return str(dest)


OPEN = STRICT = None


def setUpModule():
    global OPEN, STRICT
    STRICT = build("strict")
    OPEN = build("open", "--allow-private")


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


class Server:
    """Answers each connection with `reply(request_bytes) -> bytes` (or None for silence), and records the requests."""

    def __init__(self, reply, hold=0.0):
        self.reply = reply
        self.hold = hold
        self.requests = []
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", PORT))
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
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
            self.requests.append(data)
            out = self.reply(data)
            if self.hold:
                time.sleep(self.hold)
            if out is not None:
                conn.sendall(out)
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


def run(binary, *args, timeout=30, chosen=True):
    """Two hosts are baked, so --host is required; the tests that are not about that choose the first."""
    if chosen and "--host" not in args and "introspect" not in args:
        args = ("--host", "127.0.0.1:%d" % PORT, *args)
    r = subprocess.run([binary, *args], capture_output=True, text=True, timeout=timeout)
    return r.returncode, (json.loads(r.stdout) if r.stdout.startswith("{") else r.stdout), r.stderr


class Fetch(unittest.TestCase):
    server = None

    def serve(self, reply, **kw):
        self.server = Server(reply, **kw)
        self.addCleanup(self.server.close)
        return self.server

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
        self.assertEqual(doc["error"]["detail"]["allowed"], ["127.0.0.1:%d" % PORT, "127.0.0.1:%d" % DEAD])
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
        self.assertEqual(self.rule(run(OPEN, "post", "/")[1]), "fetch.method")
        self.assertEqual(s.requests, [])

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

    def test_introspect_authority_is_the_baked_hosts(self):
        rc, doc, _ = run(STRICT, "introspect")
        rows = {(l["name"], l["argument"]) for l in doc["authority"]["labels"]}
        self.assertEqual({r[1] for r in rows if r[0] == "net_out"}, {"127.0.0.1:%d" % PORT, "127.0.0.1:%d" % DEAD})
        names = {r[0] for r in rows}
        self.assertFalse(names & {"ffi", "net_in", "file_write", "dir_write", "exec"}, names)
        self.assertTrue(doc["authority"]["bounded"])


if __name__ == "__main__":
    unittest.main()
