#!/usr/bin/env python3
"""A minimal MCP client over stdio (docs/mcp.md §7): starts the server, initializes, lists the tool, makes the
calls it is given, and prints each answer on one line. For trying the server by hand and for the transcript in the
docs; tests/conformance/test_mcp.py drives the server itself.

    python3 scripts/mcp_client.py --server build/mcp --root DIR [--timeout-ms N] [--max-output N] CALL.json ...
    echo '{"file": "data.csv", "select": "id,bytes", "limit": 3}' | python3 scripts/mcp_client.py --server build/mcp --root DIR -

A CALL is the `arguments` object of one `table` call (a file, or `-` for standard input, one JSON object per line).
"""
import argparse
import json
import procs as subprocess  # every child in a group that is reaped: tests/conformance/procs.py
import sys


class Client:
    def __init__(self, argv):
        self.p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        self.n = 0

    def request(self, method, params=None):
        self.n += 1
        msg = {"jsonrpc": "2.0", "id": self.n, "method": method}
        if params is not None:
            msg["params"] = params
        self.p.stdin.write(json.dumps(msg).encode() + b"\n")
        self.p.stdin.flush()
        line = self.p.stdout.readline()
        if not line:
            raise SystemExit("mcp_client: the server closed its output")
        return json.loads(line)

    def notify(self, method):
        self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method}).encode() + b"\n")
        self.p.stdin.flush()

    def close(self):
        self.p.stdin.close()
        return self.p.wait()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", required=True)
    ap.add_argument("--root", required=True)
    ap.add_argument("--timeout-ms")
    ap.add_argument("--max-output")
    ap.add_argument("calls", nargs="*")
    a = ap.parse_args()
    argv = [a.server, "--root", a.root]
    for flag in ("timeout_ms", "max_output"):
        if getattr(a, flag):
            argv += ["--" + flag.replace("_", "-"), getattr(a, flag)]
    c = Client(argv)
    hello = c.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                     "clientInfo": {"name": "mcp_client", "version": "0"}})
    print("initialize ->", json.dumps(hello["result"]["serverInfo"]), hello["result"]["protocolVersion"])
    c.notify("notifications/initialized")
    listed = c.request("tools/list")["result"]["tools"]
    print("tools/list ->", [t["name"] for t in listed])
    calls = []
    for spec in a.calls:
        if spec == "-":
            calls += [json.loads(l) for l in sys.stdin if l.strip()]
        else:
            calls.append(json.loads(spec) if spec.lstrip().startswith("{") else json.load(open(spec)))
    for arguments in calls:
        r = c.request("tools/call", {"name": "table", "arguments": arguments})
        if "error" in r:
            print("call", json.dumps(arguments), "-> JSON-RPC error", json.dumps(r["error"]))
            continue
        res = r["result"]
        text = res["content"][0]["text"]
        print("call", json.dumps(arguments), "-> exit", res.get("_meta", {}).get("exit_code"), "isError", res["isError"],
              "structured" if "structuredContent" in res else "text-only", len(text), "bytes")
        print("   ", (text if len(text) < 400 else text[:400] + " ...").rstrip())
        for item in res["content"][1:]:
            print("    [%s]" % item["_meta"]["stream"], item["text"].rstrip()[:300])
    return c.close()


if __name__ == "__main__":
    sys.exit(main())
