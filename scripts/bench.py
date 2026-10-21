#!/usr/bin/env python3
"""fetch against curl: wall time and peak memory of one process doing one request, over plain HTTP and over TLS to a
loopback server in another process.

    python3 scripts/bench.py [--binary build/fetch] [--runs N] [--json out.json]

Without --binary it builds one for the two benchmark hosts (http://127.0.0.1:18090, https://localhost:18453) with a
throwaway certificate authority. Every measurement is one `fork/exec` of the tool with its output sent to /dev/null, timed
by scripts/maxrss.c with `wait4` (the child's own CPU and its peak resident set come from the same call; a launcher in C because a
child forked from Python inherits Python's 24 MB high-water mark). The server holds the response in
memory and sends it with one `sendall`, so it is not the thing being measured, but it shares the machine: read the numbers
as a comparison made here, on this machine, in this run.

What is NOT equal between the two, and said so: `fetch` answers a JSON envelope (the body escaped as a JSON string) unless
`--format text`; curl writes the bytes. `fetch` caps a body at 16,777,216 bytes, so the largest response here is 16,000,000.
"""

import argparse
import json
import os
import pathlib
import socket
import ssl
import statistics
import subprocess
import sys
import tempfile
import threading
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests" / "conformance"))
HTTP_PORT, TLS_PORT = 18090, 18453

SIZES = {"1k": 1024, "1m": 1024 * 1024, "16m": 16_000_000}


def body(n):
    line = b"the quick brown fox jumps over the lazy dog, 0123456789 abcdefghijklmnopqrstuvwxyz\n"
    return (line * (n // len(line) + 1))[:n]


def serve(context_dir):
    """Run in a child process: a threaded server for /1k /1m /16m on both ports."""
    bodies = {"/" + k: body(n) for k, n in SIZES.items()}
    ctx = None
    if context_dir:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(os.path.join(context_dir, "good.pem"), os.path.join(context_dir, "good.key"))

    def handle(conn, tls):
        try:
            if tls:
                conn = ctx.wrap_socket(conn, server_side=True)
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                data += chunk
            path = data.split(b" ", 2)[1].decode()
            b = bodies.get(path, b"not found")
            head = b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: %d\r\nConnection: close\r\n\r\n" % len(b)
            conn.sendall(head + b)
            if tls:
                conn.unwrap()
        except OSError:
            pass
        finally:
            conn.close()

    def loop(port, tls):
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", port))
        s.listen(64)
        while True:
            c, _ = s.accept()
            threading.Thread(target=handle, args=(c, tls), daemon=True).start()

    threading.Thread(target=loop, args=(HTTP_PORT, False), daemon=True).start()
    if ctx:
        threading.Thread(target=loop, args=(TLS_PORT, True), daemon=True).start()
    print("ready", flush=True)
    while True:
        time.sleep(3600)


LAUNCHER = None


def measure(argv):
    """(wall seconds, cpu seconds, peak RSS in KiB, exit status), by scripts/maxrss.c (output discarded)."""
    out = subprocess.run([LAUNCHER, *argv], capture_output=True, text=True).stdout.split()
    wall, cpu, rss, status = float(out[0]) / 1e6, float(out[1]) / 1e6, int(out[2]), int(out[3])
    return wall, cpu, rss, status, 0


def summarise(samples):
    walls = sorted(s[0] for s in samples)
    return {
        "median_ms": statistics.median(walls) * 1000,
        "min_ms": walls[0] * 1000,
        "max_ms": walls[-1] * 1000,
        "cpu_ms": statistics.median(s[1] for s in samples) * 1000,
        "rss_kib": statistics.median(s[2] for s in samples),
                "failed": sum(1 for s in samples if s[3] != 0),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--binary")
    ap.add_argument("--runs", type=int, default=0, help="override the number of runs of every case")
    ap.add_argument("--json")
    a = ap.parse_args()
    global LAUNCHER
    work = pathlib.Path(tempfile.mkdtemp(prefix="fetch-bench-"))
    LAUNCHER = str(work / "maxrss")
    subprocess.run(["cc", "-O2", "-o", LAUNCHER, str(ROOT / "scripts" / "maxrss.c")], check=True)
    import test_fetch as tf  # the certificate maker of the tests
    roots, _ = tf.make_certificates(work)
    binary = a.binary
    if not binary:
        r = subprocess.run([sys.executable, str(ROOT / "scripts" / "fetch_build.py"),
                            "--host", "http://127.0.0.1:%d" % HTTP_PORT, "--host", "https://localhost:%d" % TLS_PORT,
                            "--roots", roots, "--allow-private"], capture_output=True, text=True)
        if r.returncode != 0:
            sys.exit(r.stdout + r.stderr)
        binary = str(work / "fetch")
        subprocess.run(["cp", str(ROOT / "build" / "fetch"), binary], check=True)
    server = subprocess.Popen([sys.executable, __file__, "--serve", str(work)], stdout=subprocess.PIPE, text=True)
    assert server.stdout.readline().strip() == "ready"
    results = []
    cases = []
    for scheme in ("http", "https"):
        for size, runs in (("1k", 40), ("1m", 15), ("16m", 7)):
            cases.append((scheme, size, a.runs or runs))
    try:
        for scheme, size, runs in cases:
            if scheme == "http":
                url, host = "http://127.0.0.1:%d/%s" % (HTTP_PORT, size), "127.0.0.1:%d" % HTTP_PORT
                curl = ["curl", "-s", "-o", "/dev/null", url]
            else:
                url, host = "https://localhost:%d/%s" % (TLS_PORT, size), "localhost:%d" % TLS_PORT
                curl = ["curl", "-s", "--cacert", roots, "-o", "/dev/null", url]
            limits = ["--max-bytes", "16777216", "--timeout-ms", "60000"]
            tools = {
                "curl": curl,
                "fetch (json)": [binary, "--host", host, *limits, "get", "/" + size],
                "fetch (text)": [binary, "--host", host, *limits, "--format", "text", "get", "/" + size],
            }
            for name, argv in tools.items():
                measure(argv)  # warm the page cache and the server
                samples = [measure(argv) for _ in range(runs)]
                row = summarise(samples)
                row.update({"scheme": scheme, "size": size, "tool": name, "runs": runs})
                results.append(row)
                print("%-5s %-4s %-13s median %8.1f ms  [%7.1f..%7.1f]  cpu %8.1f ms  rss %7.0f KiB  failed %d"
                      % (scheme, size, name, row["median_ms"], row["min_ms"], row["max_ms"], row["cpu_ms"], row["rss_kib"],
                         row["failed"]), flush=True)
    finally:
        server.terminate()
    if a.json:
        pathlib.Path(a.json).write_text(json.dumps(results, indent=1))
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--serve":
        serve(sys.argv[2])
    else:
        sys.exit(main())
