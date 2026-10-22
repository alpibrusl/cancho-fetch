"""The MCP server in front of `fetch` (server/mcp.cho, scripts/mcp.py): it is built against a scratch binary directory
with a fetch build that may talk to loopback, and driven over stdio."""

import json
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
import test_fetch as tf

ROOT = tf.ROOT
BIN = pathlib.Path(tempfile.mkdtemp(prefix="fetch-mcp-"))
MCP = None


def setUpModule():
    global MCP
    tf.ROOTS, tf.CONTEXTS = tf.make_certificates(tf.TMP)
    fetch = tf.build("mcp-fetch", "--allow-private")
    shutil.copy(fetch, BIN / "fetch")
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "mcp.py"), "build", "--bin", str(BIN), "--out", str(BIN / "mcp")],
                       cwd=ROOT, capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        raise RuntimeError(r.stdout + r.stderr)
    MCP = str(BIN / "mcp")


def tearDownModule():
    shutil.rmtree(BIN, ignore_errors=True)
    tf.tearDownModule()


def rpc(*messages, flags=(), timeout=30):
    """Send the messages (dicts), return the answers in order."""
    lines = "".join(json.dumps(m) + "\n" for m in messages)
    r = subprocess.run([MCP, *flags], input=lines, capture_output=True, text=True, timeout=timeout)
    return [json.loads(l) for l in r.stdout.splitlines() if l.strip()]


def server_rule(answer):
    """The server's own refusals are text content holding the tool's error shape (no structuredContent)."""
    return json.loads(answer["result"]["content"][0]["text"])["error"]["rule"]


def call(arguments, **kw):
    return rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "fetch", "arguments": arguments}}, **kw)[0]


class Mcp(unittest.TestCase):
    def serve(self, reply, **kw):
        s = tf.Server(reply, **kw)
        self.addCleanup(s.close)
        return s

    def host(self):
        return "127.0.0.1:%d" % tf.PORT

    def test_tools_list(self):
        (a,) = rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        (tool,) = a["result"]["tools"]
        self.assertEqual(tool["name"], "fetch")
        self.assertEqual(tool["annotations"], {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True})
        schema = tool["inputSchema"]
        self.assertEqual(sorted(schema["required"]), ["method", "path"])
        self.assertEqual(schema["properties"]["max-bytes"]["maximum"], 16777216)
        self.assertEqual(schema["properties"]["timeout-ms"]["maximum"], 60000)
        self.assertFalse(schema["additionalProperties"])
        self.assertNotIn("stdin", schema["properties"])
        self.assertNotIn("root", schema["properties"])

    def test_get_through_the_server(self):
        s = self.serve(lambda req: tf.ok(b"via mcp\n"))
        a = call({"host": self.host(), "method": "get", "path": "/x"})
        self.assertFalse(a["result"].get("isError"), a)
        doc = a["result"]["structuredContent"]
        self.assertEqual((doc["data"]["status"], doc["data"]["body"]), (200, "via mcp\n"))
        self.assertTrue(s.requests[0].startswith(b"GET /x HTTP/1.1"))

    def test_post_through_the_server(self):
        s = self.serve(lambda req: b"HTTP/1.1 201 Created\r\nContent-Length: 0\r\n\r\n")
        a = call({"host": self.host(), "method": "post", "path": "/p", "body": '{"k":1}', "content-type": "application/json"})
        self.assertEqual(a["result"]["structuredContent"]["data"]["status"], 201)
        self.assertTrue(s.requests[0].endswith(b'{"k":1}'))

    def test_a_refusal_is_an_error_result_with_the_rule(self):
        s = self.serve(lambda req: tf.ok())
        a = call({"host": "example.com", "method": "get", "path": "/"})
        self.assertTrue(a["result"]["isError"])
        self.assertEqual(a["result"]["structuredContent"]["error"]["rule"], "fetch.host-not-allowed")
        self.assertEqual(s.requests, [])

    def test_unknown_and_mistyped_arguments(self):
        a = call({"method": "get", "path": "/", "url": "http://x/"})
        self.assertEqual(a["error"]["code"], -32602)
        self.assertEqual(a["error"]["data"]["rule"], "mcp.unknown-argument")
        a = call({"method": "get", "path": "/", "max-bytes": "big"})
        self.assertEqual(a["error"]["data"]["rule"], "mcp.wrong-type")
        a = call({"method": "get", "path": "/a\u0000b"})
        self.assertEqual(a["error"]["data"]["rule"], "mcp.nul-in-argument")

    def test_server_deadline_ends_a_silent_host(self):
        self.serve(lambda req: None, hold=4)
        t = time.time()
        a = call({"host": self.host(), "method": "get", "path": "/", "timeout-ms": 60000}, flags=("--timeout-ms", "600"))
        self.assertTrue(a["result"]["isError"])
        self.assertEqual(server_rule(a), "mcp.timeout")
        self.assertLess(time.time() - t, 3)

    def test_output_bound(self):
        self.serve(lambda req: tf.ok(b"z" * 100000))
        a = call({"host": self.host(), "method": "get", "path": "/"}, flags=("--max-output", "2000"))
        self.assertTrue(a["result"]["isError"])
        # It once ended as mcp.signaled in about half the runs: the tool trapped on a first read of more than 8 KiB of body
        # (docs/design.md 8.3, 8.7), not the server. One rule now.
        self.assertEqual(server_rule(a), "mcp.output-too-large")

    def test_header_injection_cannot_pass_through_the_server(self):
        s = self.serve(lambda req: tf.ok())
        a = call({"host": self.host(), "method": "get", "path": "/", "accept": "a/b\r\nX-Evil: 1"})
        self.assertEqual(a["result"]["structuredContent"]["error"]["rule"], "fetch.header-malformed")
        self.assertEqual(s.requests, [])

    def test_the_server_holds_no_network_and_no_file(self):
        r = subprocess.run([MCP, "introspect"], capture_output=True, text=True, timeout=30)
        doc = json.loads(r.stdout)
        names = {l["name"] for l in doc["authority"]["labels"]}
        self.assertFalse(names & {"net_out", "net_in", "ffi", "fs_read", "fs_write", "file_read", "file_write", "dir_read", "dir_write", "env_read"}, names)
        self.assertEqual([l["argument"] for l in doc["authority"]["labels"] if l["name"] == "exec"], [str(BIN)])
        self.assertTrue(doc["authority"]["bounded"])

    def test_generated_files_are_current(self):
        r = subprocess.run([sys.executable, str(ROOT / "scripts" / "mcp.py"), "--check"], cwd=ROOT, capture_output=True, text=True, timeout=600)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
