#!/usr/bin/env python3
"""The MCP server's tool definition, generated from `fetch introspect` (docs/mcp.md §3).

    python3 scripts/mcp.py [--bin DIR]            # write generated/mcp/tools.cho and manifests/mcp.authority.json
    python3 scripts/mcp.py --check                # fail if either is not current, or the authority is over its ceiling
    python3 scripts/mcp.py build --bin DIR --out FILE

`cancho build` first: `introspect` of build/fetch is the only description of
the tool read. What is written is one cancho module, `mcp.tools`: the
`tools/list` result as one literal; the tables a call is checked against (the
properties that become flags, the operands with their counts); the bounds and
the sentences the server says when it ends a call; and `introspect`, the
server's description of itself, which holds the server's own authority as the
compiler derives it (the same fixed point as scripts/manifest.py: embedding the
report must not change the report).

Adapted from cancho-tools' scripts/mcp.py (rev 74411ee43c74a2741ed1bb37404844fcbb031753): one tool, the property
kinds of this tool's flags (bool, nat, text, path, choice), `maximum` from
`limits`, no standard input, an authority gate for the server, and the
server's introspection.

The server holds `Exec` narrowed to the directory of the binary, and `narrow`
takes a literal (docs/mcp.md §5), so the directory is in the source. The
committed module and server say `/opt/cancho-fetch/bin`; `build --bin` writes
both with another directory into a scratch copy and builds that. Nothing a
model sends reaches the directory: there is one name.
"""

import argparse
import json
import pathlib
import procs as subprocess  # every child in a group that is reaped: tests/conformance/procs.py
import sys
import tempfile
import tomllib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import manifest  # noqa: E402

ROOT = manifest.ROOT
DEFAULT_BIN = "/opt/cancho-fetch/bin"
GENERATED = ROOT / "generated" / "mcp" / "tools.cho"
AUTHORITY = ROOT / "manifests" / "mcp.authority.json"
SERVER = ROOT / "server" / "mcp.cho"
CEILING = ROOT / "server" / "ceiling.toml"
VERSION = "0.1.0"

# The server's own bounds (docs/mcp.md §4), written once here and read by the
# server from the generated module: `introspect` and the code cannot disagree.
DEFAULT_TIMEOUT_MS = 30000
DEFAULT_MAX_OUTPUT = 2097152     # 2 MiB: a default json page (--max-bytes 1 MiB) and its envelope always fit
DEFAULT_MAX_INPUT = 1048576      # 1 MiB: the `stdin` text of one call (docs/mcp.md section 12); the request line bounds it from above
MAX_LINE = 4194304               # a request line longer than this is refused
MAX_STDERR = 65536               # the tool's standard error, kept up to this much

# Flags the server sets: `--root` is the server's (role `root`).
WITHHELD_ROLES = {"root"}
WITHHELD_FLAGS = set()


def introspect(tool, build_dir):
    out = subprocess.run([str(pathlib.Path(build_dir) / tool), "introspect"],
                         check=True, capture_output=True).stdout
    return json.loads(out)


def property_of_operand(name):
    """`FILE` -> `file`, `FILE...` -> `files`."""
    base = name.rstrip(".").lower()
    return base + "s" if name.endswith("...") else base


def ceilings(d):
    return {l["name"]: l["ceiling"] for l in d.get("limits", [])}


def schema_of_flag(flag, ceiling):
    kind = flag["kind"]
    if kind == "bool":
        s = {"type": "boolean"}
    elif kind == "nat":
        s = {"type": "integer", "minimum": 0}
        name = flag["name"][2:]
        if name in ceiling:
            s["maximum"] = ceiling[name]
    elif kind == "hex64":
        s = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
    elif kind.startswith("choice:"):
        s = {"enum": kind[len("choice:"):].split("/")}
    elif kind in ("text", "any", "path"):
        s = {"type": "string"}
        if kind == "text":
            s["minLength"] = 1
    else:
        sys.exit("mcp: flag %s has a kind this script does not know: %s" % (flag["name"], kind))
    s["description"] = flag["help"]
    if flag["default"] is not None:
        s["default"] = int(flag["default"]) if kind == "nat" else flag["default"]
    return s


HINT_INPUT = "this tool takes no stdin; the bound is the server's --max-input"


def hints(props):
    """What the server says when it ends a call, naming properties the tool has:
    the generator fails if one of them is gone, rather than say something false."""
    for p in ("max-bytes", "timeout-ms", "host"):
        if p not in props:
            sys.exit("mcp: the hints name `%s`, which `introspect` no longer has" % p)
    too_large = ("ask for less: the response body is capped by `max-bytes` (up to the tool's ceiling); the server's "
                 "--max-output bounds the whole answer, so raise neither past what the task needs")
    timeout = ("the host did not answer in time: `timeout-ms` is the tool's own deadline (up to 60000) and is shorter than "
               "the server's --timeout-ms; try the host later")
    return too_large, timeout


def definition(d):
    """The tool's `tools/list` entry, and the tables the server checks a call against."""
    name = d["tool"]
    props, required, flags, operands = {}, [], [], []
    ceiling = ceilings(d)
    for f in d["flags"]:
        if f["role"] in WITHHELD_ROLES or f["name"] in WITHHELD_FLAGS:
            continue
        prop = f["name"][2:]
        props[prop] = schema_of_flag(f, ceiling)
        flags.append((prop, f["name"], "bool" if f["kind"] == "bool" else "nat" if f["kind"] == "nat" else "text"))
    # A tool that reads standard input when its operand is absent or `-` (table, docs/readers.md section 3): the server offers it a `stdin` property, the text
    # it writes to the pipe that is the child's standard input (docs/mcp.md section 12), and a call gives exactly one of the operand and `stdin`, which the
    # server checks, so neither is `required` in the schema. A tool that reads standard input and has no such operand is still refused: nothing here would
    # tell a caller what its input is.
    reads_stdin = "io_read" in d["authority"]["effects"]
    if reads_stdin and not any(o["min"] == 0 and o["max"] == 1 for o in d["operands"]):
        sys.exit("mcp: %s reads standard input, which this generator does not offer" % name)
    for o in d["operands"]:
        prop = property_of_operand(o["name"])
        if prop in props:
            sys.exit("mcp: %s's operand %s and a flag are both `%s`" % (name, o["name"], prop))
        least = o["min"]
        helptext = o["help"]
        if reads_stdin and o["min"] == 0 and o["max"] == 1:
            helptext += " Give exactly one of `%s` and `stdin`." % prop
        if o["max"] == 1:
            s = {"type": "string", "description": helptext}
        else:
            s = {"type": "array", "items": {"type": "string"}, "description": helptext}
            if least > 0:
                s["minItems"] = least
            if o["max"] is not None:
                s["maxItems"] = o["max"]
        props[prop] = s
        if least > 0:
            required.append(prop)
        operands.append((prop, least, "" if o["max"] is None else o["max"]))
    if reads_stdin:
        if "stdin" in props:
            sys.exit("mcp: %s has a flag called `stdin`" % name)
        props["stdin"] = {"type": "string", "description": STDIN_HELP}
    for table in (flags, operands):
        for row in table:
            for field in row:
                if any(c in str(field) for c in ";|"):
                    sys.exit("mcp: %s: `%s` would split the table" % (name, field))
    # The flag that chooses the format: the answer is the JSON document only when it is `json`.
    selector = ""
    fmt = props.get("format")
    if fmt is not None and "json" in fmt.get("enum", []) and d["output"] == "document":
        selector = "format"
    effects = set(d["authority"]["effects"])
    read_only = not (effects & {"file_write", "dir_write", "net_out", "net_in", "exec", "ffi"})
    entry = {
        "name": name,
        "description": d["summary"] + DESCRIPTION_SUFFIX,
        "inputSchema": {"type": "object", "properties": props, "required": required,
                        "additionalProperties": False},
        "annotations": {"readOnlyHint": read_only, "destructiveHint": not read_only,
                        "idempotentHint": bool(d.get("guarantees", {}).get("idempotent")),
                        "openWorldHint": "net_out" in effects},
    }
    return entry, {
        "name": name,
        "flags": ";".join("|".join(map(str, r)) for r in flags),
        "operands": ";".join("|".join(map(str, r)) for r in operands),
        "stdin": "text" if reads_stdin else "",
        "output": d["output"],
        "structured": selector,
    }, props


# The property `stdin` (docs/mcp.md of cancho-table, section 12) is offered only to a tool that reads standard input; `fetch` does not.
STDIN_HELP = "Not offered by this tool."

# Appended to the tool's own summary: what the server adds.
DESCRIPTION_SUFFIX = (
    "\n\nThe hosts this tool can reach are baked into it; `host` selects among them and any other value is refused with the "
    "list. The answer is the tool's own output, byte for byte, and is bounded by the server. A refusal is an error result "
    "whose JSON names a `rule` and a `repair`; match on the rule. Redirects are not followed: a 3xx status is data.")


def server_doc(d, props, authority, bin_dir, pin):
    """What `mcp introspect` prints."""
    too_large, timeout = hints(props)
    too_large_input = HINT_INPUT
    return {
        "tool": "mcp",
        "version": VERSION,
        "compiler": pin,
        "summary": "An MCP server (stdio, JSON-RPC 2.0) that serves the tool `fetch`: its input schema is "
                   "derived from the tool's `introspect`, a call is a fixed argv with no shell, the answer is the tool's "
                   "stdout, stderr and exit code unchanged, bounded in size and in time.",
        "usage": "mcp [--timeout-ms N] [--max-output N] [--max-input N] | mcp introspect",
        "output": "stdio",
        "protocol": {"transport": "stdio, one JSON-RPC 2.0 message per line", "version": "2025-06-18",
                     "methods": ["initialize", "ping", "tools/list", "tools/call"],
                     "notifications": "accepted and not answered",
                     "max_line_bytes": MAX_LINE},
        "serves": {"tool": d["tool"], "version": d["version"], "compiler": d["compiler"], "bin": bin_dir},
        "tools": [d["tool"]],
        "flags": [
            {"name": "--timeout-ms", "kind": "nat", "role": "none", "default": DEFAULT_TIMEOUT_MS,
             "help": "the deadline of one call: the tool is killed past it and the call ends as mcp.timeout"},
            {"name": "--max-output", "kind": "nat", "role": "none", "default": DEFAULT_MAX_OUTPUT,
             "help": "the most standard output of one call, in bytes: the tool is killed past it and the call ends as "
                     "mcp.output-too-large"},
            {"name": "--max-input", "kind": "nat", "role": "none", "default": DEFAULT_MAX_INPUT,
             "help": "the most text of one call's `stdin` property, in decoded bytes: past it the call is refused before the tool starts, as "
                     "mcp.input-too-large"},
        ],
        "limits": [
            {"name": "timeout-ms", "default": DEFAULT_TIMEOUT_MS},
            {"name": "max-output", "default": DEFAULT_MAX_OUTPUT},
            {"name": "max-input", "default": DEFAULT_MAX_INPUT, "note": "the `stdin` text of a call, in decoded bytes; the request line (below) bounds it from above"},
            {"name": "stderr", "default": MAX_STDERR, "note": "fixed: the tool's standard error beyond it ends the call as mcp.output-too-large"},
            {"name": "line", "default": MAX_LINE, "note": "fixed: a request line beyond it is refused as mcp.line-too-long"},
        ],
        "rules": [
            {"rule": "mcp.parse-error", "answer": "jsonrpc -32700", "summary": "the line is not one JSON value"},
            {"rule": "mcp.invalid-request", "answer": "jsonrpc -32600", "summary": "not a JSON-RPC 2.0 request"},
            {"rule": "mcp.line-too-long", "answer": "jsonrpc -32600", "summary": "a request line longer than max_line_bytes"},
            {"rule": "mcp.unknown-method", "answer": "jsonrpc -32601", "summary": "a method other than initialize, ping, tools/list, tools/call"},
            {"rule": "mcp.unknown-tool", "answer": "jsonrpc -32602", "summary": "a tool name other than %s" % d["tool"]},
            {"rule": "mcp.arguments-not-object", "answer": "jsonrpc -32602", "summary": "`arguments` is not an object"},
            {"rule": "mcp.unknown-argument", "answer": "jsonrpc -32602", "summary": "a property the input schema does not have (data lists the real ones)"},
            {"rule": "mcp.wrong-type", "answer": "jsonrpc -32602", "summary": "a property of the wrong type; every fault is listed in data.problems"},
            {"rule": "mcp.nul-in-argument", "answer": "jsonrpc -32602", "summary": "a NUL byte in a string, which would arrive as two arguments"},
            {"rule": "mcp.input-conflict", "answer": "jsonrpc -32602", "summary": "both `file` and `stdin` given: the call has one input (data.properties names them)"},
            {"rule": "mcp.input-missing", "answer": "jsonrpc -32602", "summary": "neither `file` nor `stdin` given: the call has no input (data.properties names them)"},
            {"rule": "mcp.input-too-large", "answer": "isError result", "summary": "the `stdin` text passed --max-input; " + too_large_input},
            {"rule": "mcp.timeout", "answer": "isError result", "summary": "the call ran past --timeout-ms; " + timeout},
            {"rule": "mcp.output-too-large", "answer": "isError result", "summary": "the output passed --max-output (or the error stream passed its bound); " + too_large},
            {"rule": "mcp.signaled", "answer": "isError result", "summary": "the tool was ended by a signal"},
            {"rule": "mcp.spawn", "answer": "isError result", "summary": "the tool could not be started or watched"},
        ],
        "confinement": {"network": "the server holds no Net and reads no file: only the tool reaches the network, for the hosts baked into it (the tool's introspect lists them), and flag values arrive as one `--name=value` argument and operands after `--`, with no shell",
                        "exec": "the binary of the directory its Exec is narrowed to (baked at build time), by one fixed name"},
        "authority": authority,
    }


def tools_cho(entry, table, props, d, authority, bin_dir, pin):
    lit = manifest.literal
    too_large, timeout = hints(props)
    doc = server_doc(d, props, authority, bin_dir, pin)
    out = ["""edition 5;

module mcp.tools;

// Generated by scripts/mcp.py from the tool's `introspect` and the compiler's
// authority report of the server -- do not edit (docs/mcp.md §3). The
// `tools/list` result, and for tool `i` the tables a call is checked against:
// its properties as flags (`property|flag|kind`, kind `bool`, `nat` or `text`),
// its operands in order (`property|min|max`, `max` empty for unbounded), how
// standard input reaches it (none, or `text`: a property called `stdin`), its output (`document` or `stream`), and the
// property that chooses its format (the answer is structured content only when
// it is `json`). Then the server's bounds and the sentences it says when it
// ends a call, and `introspect`.

pub fn list_result() -> [] &static [byte] {
    return %s;
}

pub fn count() -> [] int {
    return 1;
}
""" % lit(manifest.compact({"tools": [entry]}))]
    for field in ("name", "flags", "operands", "stdin", "output", "structured"):
        out.append("\npub fn %s(i: int) -> [] &static [byte] {\n" % field)
        out.append("    if i == 0 {\n        return %s;\n    }\n" % lit(table[field]))
        out.append('    return "";\n}\n')
    out.append("\n// The binary, beneath the directory the server's `Exec` is narrowed to.\npub fn path(i: int) -> [] &static [byte] {\n")
    out.append("    if i == 0 {\n        return %s;\n    }\n" % lit("%s/%s" % (bin_dir, table["name"])))
    out.append('    return "";\n}\n')
    out.append("""
pub fn default_timeout() -> [] int {
    return %d;
}

pub fn default_output() -> [] int {
    return %d;
}

pub fn default_input() -> [] int {
    return %d;
}

pub fn longest_line() -> [] int {
    return %d;
}

pub fn most_errors() -> [] int {
    return %d;
}

// What the server tells a caller whose call it had to end (the `hint` of the error).
pub fn hint_too_large() -> [] &static [byte] {
    return %s;
}

pub fn hint_timeout() -> [] &static [byte] {
    return %s;
}

pub fn hint_input() -> [] &static [byte] {
    return %s;
}

// `mcp introspect`.
pub fn introspect() -> [] &static [byte] {
    return %s;
}
""" % (DEFAULT_TIMEOUT_MS, DEFAULT_MAX_OUTPUT, DEFAULT_MAX_INPUT, MAX_LINE, MAX_STDERR, lit(too_large), lit(timeout), lit(HINT_INPUT),
       lit(manifest.compact(doc))))
    return "".join(out)


def check_bin(bin_dir):
    p = pathlib.PurePosixPath(bin_dir)
    if not bin_dir.startswith("/") or str(p) != bin_dir or bin_dir == "/" or any(c in bin_dir for c in '"\\\n'):
        sys.exit("mcp: --bin must be an absolute, normalised directory other than /")


def baked(text, bin_dir):
    """The server's source with its directory replaced: every `Exec(...)`,
    `exec(...)` and `narrow` names it."""
    return text.replace('"%s' % DEFAULT_BIN, '"%s' % bin_dir)


def derive_server(server_text, tools_text):
    with tempfile.TemporaryDirectory() as scratch:
        s = pathlib.Path(scratch)
        (s / "mcp.cho").write_text(server_text)
        (s / "tools.cho").write_text(tools_text)
        return manifest.derive([str(s / "mcp.cho"), str(s / "tools.cho")])


def generated(build_dir, bin_dir):
    """`(tools.cho text, the server's authority report)`: the report is derived with the module that embeds it, and
    must be the same report after (the fixed point)."""
    pin = manifest.project()["package"]["cancho"]
    tool = manifest.project()["bin"][0]["name"]
    d = introspect(tool, build_dir)
    entry, table, props = definition(d)
    server_text = baked(SERVER.read_text(), bin_dir)
    first = derive_server(server_text, tools_cho(entry, table, props, d, {}, bin_dir, pin))
    text = tools_cho(entry, table, props, d, first, bin_dir, pin)
    second = derive_server(server_text, text)
    if second != first:
        sys.exit("mcp: no fixed point -- embedding the authority changed the authority")
    return text, first


def gate(authority, bin_dir):
    """The server's row within its ceiling (server/ceiling.toml), as scripts/manifest.py gates the tool's."""
    problems = []
    with open(CEILING, "rb") as f:
        ceiling = tomllib.load(f)["mcp"]
    allowed = set(ceiling["allow"])
    if not authority.get("bounded", False):
        problems.append("mcp: bounded is false")
    if authority.get("foreign_symbols"):
        problems.append("mcp: foreign symbols %s" % authority["foreign_symbols"])
    for label in authority["labels"]:
        text = manifest.label_text(label)
        if label["name"] == "exec" and label["argument"] == bin_dir:
            text = manifest.label_text({"name": "exec", "argument": DEFAULT_BIN})   # a baked build's directory is its own
        if label["name"] in ceiling["forbid"]:
            problems.append("mcp: %s is forbidden to the server" % text)
        if text not in allowed:
            problems.append("mcp: %s is not within the ceiling in server/ceiling.toml" % text)
        if label["name"] == "exec" and label["argument"] != bin_dir:
            problems.append("mcp: exec is narrowed to %r, not the binary directory %r" % (label["argument"], bin_dir))
    return problems


def build(build_dir, bin_dir, out):
    check_bin(bin_dir)
    text, authority = generated(build_dir, bin_dir)
    problems = gate(authority, bin_dir)
    if problems:
        sys.exit("\n".join(problems))
    with tempfile.TemporaryDirectory() as scratch:
        src = pathlib.Path(scratch)
        (src / "tools.cho").write_text(text)
        (src / "mcp.cho").write_text(baked(SERVER.read_text(), bin_dir))
        subprocess.run([manifest.compiler(), "build", str(src / "mcp.cho"), str(src / "tools.cho"), "--std", "-o", out],
                       check=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("command", nargs="?", choices=["build"])
    ap.add_argument("--bin", default=None)
    ap.add_argument("--build-dir", default=str(ROOT / "build"))
    ap.add_argument("--out")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    if a.command == "build":
        if not a.bin or not a.out:
            sys.exit("mcp: build needs --bin and --out")
        build(a.build_dir, a.bin, a.out)
        return
    bin_dir = a.bin or DEFAULT_BIN
    text, authority = generated(a.build_dir, bin_dir)
    record = json.dumps(authority, indent=2) + "\n"
    problems = gate(authority, bin_dir)
    if a.check:
        if not GENERATED.exists() or GENERATED.read_text() != text:
            problems.append("mcp: generated/mcp/tools.cho is not what introspect and the compiler say; run scripts/mcp.py")
        if not AUTHORITY.exists() or AUTHORITY.read_text() != record:
            problems.append("mcp: manifests/mcp.authority.json is not the compiler's report; run scripts/mcp.py")
        for p in problems:
            print("FAIL " + p)
        if problems:
            sys.exit(1)
        print("mcp     %s" % ", ".join(manifest.label_text(l) for l in authority["labels"]))
        return
    GENERATED.parent.mkdir(parents=True, exist_ok=True)
    GENERATED.write_text(text)
    AUTHORITY.write_text(record)
    for p in problems:
        print("FAIL " + p)
    if problems:
        sys.exit(1)


if __name__ == "__main__":
    main()
