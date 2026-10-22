# `fetch`: HTTP to a baked list of hosts, with the list in the authority row

> **Status: design, nothing built.** Written before the code. Claims are marked **read** (from a file or a document, cited),
> **measured** (run, with the result stated) or **assumed** (to be measured at the step named). This repository is separate
> from `cancho-tools` on purpose (§1); it follows that repository's contract, so a host can treat the two as one family.
> Depends on cancho's exact host matching (`docs/net-bounds.md` §2.3, edition 10, alpibrusl/cancho#561); until that is
> merged this document's central claim is not true of the compiler.

---

## 1. What this is, and why it is not in `cancho-tools`

`fetch` makes one HTTP request and answers one envelope. Its difference from `curl` is not the protocol; it is that the
hosts it may reach are a **literal in the binary**, shown by `cancho authority` as one `net_out` label per host, and
nothing else in the binary can reach the network. A reviewer reads the row instead of trusting a flag.

`cancho-tools` sells "none of these tools can reach the network" as a checked property: `scripts/manifest.py` forbids
`ffi`, `net_out`, `net_in` and `clock`, and excuses only `clock`, only where `tools.toml` lists it with a reason (read,
`docs/archive.md` §9). A tool that holds `net_out` would turn that sentence into "except one". Here the ceiling file says
the opposite: **`net_out` is allowed, `ffi`, `net_in` and every unnamed host are not**, and the gate is the same program.

Inherited from `cancho-tools` unchanged: one binary per tool; the `contract/` package; the envelope (`ok`, `data`,
`refusals[]` each with a rule tag and a `repair`); a flag table that is checked for well-formedness; a `tools.toml` ceiling
per tool; schemas from `scripts/schemas.py`; the MCP server; mutation testing that rebuilds the real binary afterwards. How
this repository takes them (the published package of `cancho-tools`, `scripts/package.py`, versus a copy) is open
question 1.

## 2. What cancho already gives, and what it does not

| piece | what it gives | what it does not (or what is unverified) |
|---|---|---|
| `tcp_connect(net, name, port)` | **read** (`docs/connect.md` §10, `crates/cancho-codegen/src/body/net.rs`): the name and port are checked against the `Net` bound **before** the name is resolved; resolution is the C library's `getaddrinfo` inside cancho's runtime, so `Ffi` is not in the report | the check is on the **name**, not the address it resolves to (§5.3) |
| exact host bounds | edition 10, `*.suffix` for one label (alpibrusl/cancho#561); **measured** there on both backends with IP literals | not merged at the time of writing |
| several bounds | `narrow(net, "a:443", "b:443", ...)` yields one child per host, each its own label in the report (**measured**, cancho#553); `Net` containment is host-exact and port-exact (#560, #561) | each child is a separate `res` value; a request picks the child by host, at run time, by `match` |
| `packages/tls` | **read** (`docs/tls-pure.md`, `tls-core.md`): TLS 1.3 client engine in cancho, bytes in and bytes out, no C library; `packages/x509` verifies the chain against roots the caller gives, the name and the time | its own status line is "not independently reviewed (#209)"; one client slot is 269,439 bytes (**read**, `docs/http-client.md` §2) |
| `packages/http-client` | **read** (`docs/http-client.md`): HTTP/1.1 client with strict response framing and the refusal tags a gateway and a webhook sender already agreed on | designed for a non-blocking multi-request loop; a one-shot tool uses a small part of it (open question 4) |
| `conn_peer(&Conn, &![byte])` | **read** (`docs/conn-peer.md`): the address of the other end of a connection, no effect row | **assumed** to work on a client connection; measured at step 1 |
| a deadline | `clock_ms`; needs `clock`, so the same excuse `run` has in `tools.toml` | `getaddrinfo` blocks and a deadline cannot interrupt it (§5.5) |
| randomness | the engine is seeded once from `/dev/urandom` through an `Fs("/dev/urandom")` (**read**, `docs/tls-pure.md` §6); the row shows `fs_read("/dev/urandom")` | |

## 3. The tool

```
fetch get  <path> [--host H] [--header NAME=VALUE]... [--max-bytes N] [--timeout-ms N] [--max-redirects N]
fetch head <path> [--host H] ...
fetch post <path> [--host H] --body-stdin | --body TEXT ...
```

* **The host is not an argument that can name a new host.** `--host` selects among the baked ones (it may be omitted when
  there is exactly one). A value outside the list is `fetch.host-not-allowed`, with the baked list in `data` so the caller
  can repair. A **full URL** is not accepted in v1: `fetch get https://elsewhere/x` is `fetch.url-not-a-path`. Reason: a
  URL is a second place a host can be written, and the only host that matters is the baked one.
* **The scheme is part of the baked entry, not of the request.** An entry is `scheme://host[:port]` (`http://` is built
  in step 1, `https://` arrives with step 2); the port defaults to the scheme's. There is no flag for the scheme, so a
  caller cannot downgrade. (This replaces an earlier draft in which the scheme followed the port: a service on
  `:8443` or `:8080` is ordinary, and a rule that guesses the scheme from a number is a rule that is wrong somewhere.)
* **Request headers** are an allow-list (`Accept`, `Accept-Language`, `Content-Type`, `If-None-Match`, `If-Modified-Since`,
  `User-Agent`, `X-*`). `Authorization`, `Cookie`, `Proxy-*` and `Host` are not accepted from the caller at all (there is no flag for a header but
  `--accept`, `--content-type`); a credential is the operator's, built in (8.6).
* **Response** envelope: `status`, `headers` (an allow-list, lowercased), `body` (text if valid UTF-8, else base64 with a
  `body_encoding` field), `truncated` (the cap was hit), `redirects` (each hop), `peer` (the address connected to),
  `tls` (version, cipher, whether the chain verified).
* **Redirects** are followed up to `--max-redirects` (default 0). A redirect to a host not in the list is refused as
  `fetch.redirect-not-allowed` with the target in `data`; a redirect to the same host is followed; a `Location` that is not a
  path or a baked host is refused. Nothing is "followed and then checked".
* **Bounds**: `--max-bytes` (default 1 MiB, ceiling in `tools.toml`), `--timeout-ms` (default 10,000, ceiling 60,000).
  Exceeding the body cap is `fetch.body-too-large` with the bytes read so far in `data`.
* Class (`contract/describe.cho`): `irreversible-consequential` for all three verbs. A `GET` can have effects off the
  machine, and the envelope class is about what the caller can undo. Opt-in in the MCP server, as `run` is (`--with fetch`).

### 3.1 Held to the agent-first list

`fetch` is held to the same checked list as the tools in `cancho-tools` (`docs/agent-first.md` there, enforced by
`tests/conformance/test_agent_first.py`), with no exception:

* **Self-describing**: `introspect` and `skill` carry every flag, operand, rule, limit and guarantee, with help text on all
  of them. The baked hosts are in `introspect` (`hosts`) and in the MCP `inputSchema` as the `enum` of `--host`, so a model
  is told which hosts it may use before it tries one.
* **Every refusal is data and says what to do.** One JSON record on standard output, nothing on standard error, a rule
  from the tool's own table, the exit status the table promises, and a non-empty `hint`. Where the repair is mechanical it
  is a `repair` (`fetch.host-not-allowed` carries `retry` with each baked host; `fetch.redirect-not-allowed` carries the
  target in `detail` so the caller can decide; `fetch.body-too-large` carries the bytes read and `--max-bytes`'s ceiling).
* **Byte-identical whatever the environment** (locale, zone, terminal): the envelope carries no timestamps from the
  machine and no resolver-dependent text; the connected peer address is data, not prose.
* **MCP annotations from the description**: `readOnlyHint` false (a request can have effects off the machine, even a
  `GET`), `destructiveHint` true, `idempotentHint` true for `get` and `head` and false for `post`, `openWorldHint` true.
  A host that gates on them will confirm a `fetch` call, which is the intended default.
* **The refusals a fetch can hit are listed up front** (so the rule table is complete before any code): `fetch.host-not-allowed`,
  `fetch.url-not-a-path`, `fetch.path-malformed`, `fetch.header-forbidden`, `fetch.redirect-limit`,
  `fetch.redirect-not-allowed`, `fetch.body-too-large`, `fetch.timeout`, `fetch.peer-not-public`, `fetch.tls-chain`,
  `fetch.tls-name`, `fetch.tls-expired`, `fetch.tls-protocol`, `fetch.response-*` (the framing tags), `fetch.connect-failed`
  (with the errno) and the shared `args.*`, `path.*`, `limit.*` rules.

What is not checked, as there: whether a model picks the right host or flag from the description, and whether the
envelope fits a context window; both are empirical.

Not offered, each with a tag and a pointer to `curl`: other methods (`PUT`, `DELETE`, `PATCH`), proxies, cookies, HTTP/2,
client certificates, `Range`, compression (`Accept-Encoding: identity` is sent), uploads of files, a bare IP as a host.

## 4. The authority row (read; the numbers are for step 1)

For a build with hosts `api.example.com:443` and `static.example.com:443`:

```
performs
    io_write
    io_read                       (only if --body-stdin is offered)
    net_out("api.example.com:443")
    net_out("static.example.com:443")
    fs_read("/dev/urandom")       (the TLS seed)
    fs_read("<roots bundle>")     (the trust anchors; or none if they are baked, open question 3)
    clock                         (the deadline)
bounded: true     unbounded_by: []     ffi: none     net_in: none
```

The ceiling file, `tools.toml`, would say: `net_out` allowed with exactly the baked hosts (the gate compares the report's
labels to the list `fetch_build.py` was given, so a source edit that adds a host fails the gate even if the compiler would
accept it); `clock` excused with the reason "deadline"; `fs_read` allowed for the two paths above only; `ffi`,
`net_in`, `exec`, `fs_write` forbidden. **A host removed from the list shrinks the row; a host added changes the build
input, not just the source.**

## 5. What can go wrong, and the rule for each

### 5.1 Redirects and URLs
Covered in §3. Tags: `fetch.url-not-a-path`, `fetch.host-not-allowed`, `fetch.redirect-limit`,
`fetch.redirect-not-allowed`. Userinfo in a path-like input (`//user@host/`) and a `\` or control byte in the path are
`fetch.path-malformed`; the request line is built from bytes the tool has validated, never from the caller's text.

### 5.2 Response framing
A response with two lengths, a length and `Transfer-Encoding`, an unknown transfer coding, a head over its bound, obsolete
line folding or a malformed status line is refused with the tags `packages/http-client` already defines (`response.*`,
**read**), re-prefixed `fetch.response-*`. A body framed by connection close is accepted only up to `--max-bytes` and
marked `truncated: false` only when the close was clean.

### 5.3 The name is checked, not the address (the SSRF hole that remains)
The bound admits a **name**; the resolver chooses the address. `api.example.com` can resolve to `169.254.169.254` if its
zone says so, or to a loopback address after the allow-list was reviewed (DNS rebinding). cancho's check cannot see this
(**read**: it runs before resolution). Mitigations, in order of strength:

1. **Baked entries may be IP literals** (`203.0.113.7:443`), which are exact bounds on an address and make the question
   vanish. TLS then verifies the name through an SNI/hostname given separately (`--sni` baked alongside; open question 5).
2. After `tcp_connect` succeeds, `conn_peer` is read and a **private, loopback, link-local or unique-local peer** is
   `fetch.peer-not-public` unless the build was told `--allow-private` (a ceiling-file switch, off by default). The socket is
   closed before a byte is sent. This does not undo the TCP handshake to an internal address; it prevents the request and the
   response from crossing it. That is a reduced guarantee and it is named as one.
3. The report cannot show the resolver. **Not claimed**: that the machine only talks to the baked hosts at the packet level.
   Claimed: that this binary only *connects to* the baked names, and refuses a connection that resolves to a non-public peer.

### 5.4 TLS
`fetch.tls-*` tags for each failure class the engine reports (chain, name, expiry, protocol). No option disables
verification and none is proposed. The caveat of #209 (the TLS stack has not been independently reviewed) is printed in the
README and in `describe`, not buried here: an agent fetching secrets over it is taking that risk. Plain HTTP entries
(`host:80`) are allowed and the envelope says `tls: null`.

### 5.5 Time
The deadline is checked between reads. **A name that is slow to resolve is not bounded by it** (`getaddrinfo` blocks;
**assumed**, to be measured at step 1 against an unreachable resolver). The honest statement is "the request is bounded after
the name resolves"; the host's own kill timeout is the outer bound. If that is not enough, the alternative is to bake IP
literals (§5.3, 1).

### 5.6 Memory
`--max-bytes` bounds the body and a fixed head buffer bounds the headers. One TLS slot is 269 KB (read). The tool should be
able to claim `bounded_memory`; **measured at step 2** with the launcher `cancho-tools` uses for maxrss (not Python's
`wait4`, which inherits the runner's peak).

## 6. The build model

`cancho` takes `narrow` literals at compile time (`docs/narrowing-into-several.md`; there is no `Dir` for `Net`). So:

* `scripts/fetch_build.py --host api.example.com:443 --host static.example.com:443` writes `generated/fetch/hosts.cho` (the
  baked list as `narrow(net, ...)` plus a `match` from host name to child) and builds the binary.
* **One binary per allow-list.** A deployment that wants a different list builds a different binary. For a security tool
  that is the point; for ergonomics it is a build per allow-list, and the repository ships the script and a Dockerfile.
* The list is also written into `describe` and into the MCP tool's `inputSchema` (`--host` becomes an `enum`), so a model sees
  exactly the hosts it may use.
* The ceiling gate compares `cancho authority`'s `net_out` labels with the list given to the build.

## 7. Tests (stated before the code)

No test touches the Internet.

1. **Loopback server fixtures**: a plain HTTP server and a TLS server (from `examples/tls_echo` and `packages/http-server`)
   on `127.0.0.1`, with a CA made for the tests. Every refusal tag has a fixture that reaches it.
2. **The allow-list**: a host not in the list is refused before any socket opens (strace-style check: no `connect` call);
   `127.0.0.10` with a baked `127.0.0.1` is refused (the edition-10 case); `*.localhost`-style wildcards if the build offers them.
3. **Redirects**: to the same host, to another baked host, to an unlisted host, a loop, and a `Location` with a scheme change.
4. **Rebinding**: a baked name that resolves (through a test `hosts` entry or a resolver on loopback) to `127.0.0.1` is
   refused by `fetch.peer-not-public` without `--allow-private`, and allowed with it.
5. **Framing and hostile responses**: oversized head, two lengths, chunked with a bad size, a body that never ends, a server
   that closes mid-body. Memory is measured in the hostile cases.
6. **The authority row is pinned**: the exact labels for a fixture build; a mutant that adds a host to the source but not the
   build list fails the gate.
7. **Mutation testing** (as `run_mutants.py` does) over the request builder, the header allow-list, the redirect logic, the peer
   check and the framing; survivors become tests; the real binary is rebuilt after.
8. **Schemas, MCP, flag table** well-formedness, as in `cancho-tools`.

## 8. Order

0. cancho#561 merged (exact hosts). Scaffolding: `cancho.toml` pin, `tools.toml`, the gate, CI.
1. **Plain HTTP `GET`/`HEAD` to a baked host on loopback.** Measure: the report, `conn_peer` on a client connection, the
   resolver-blocking claim (§5.5), the row.
2. **HTTPS** with roots and the seed; the #209 caveat in the README; memory measured.
3. **`POST`** with a body from stdin or `--body`.
4. **MCP** (`--with fetch`), schemas, the `enum` of hosts.
5. Redirects (a separate step so its tests are its own).

`ssh` is not here (cancho-tools `docs/archive.md` §9: no design, no asker, and the policy question "what may the agent do on
the other side" has no answer a row can show).

## 9. Open questions for the maintainer

1. **Share or copy `cancho-tools`' scaffolding?** Depend on its published package (`scripts/package.py`) for `contract/` and the
   envelope, or copy the minimum and keep the shapes identical? Dependence is less drift; copying is less coupling.
2. **Credentials.** *Decided and built (8.6): a file per https host, named at build time.* The options this question listed were
   wrong in one place: "a header whose value is read from an environment variable ... the row would show no new label,
   since env comes in `args`" is **false**. A cancho program has no `getenv` without `Ffi` (`docs/agent-toolbox.md`
   section on determinism: "no environment ... the language has no `getenv`"), and `Args` carries the command line, not the
   environment. An environment variable would need `Ffi`, which the ceiling forbids. A file is the channel the row can name.
3. **Trust roots.** `fs_read("<bundle>")` in the row, or roots baked into the binary at build (no `fs_read`, a larger binary, a
   rebuild to rotate)? Baking makes the row smaller and the rotation story worse.
4. **`packages/http-client` or a small request writer and the existing framing rules?** The package is built for many requests
   on one poller; a one-shot tool might be simpler with a blocking path, but then the framing rules are written twice.
5. **IP-literal entries with a separate SNI/name** (§5.3, 1): worth offering in v1, or only after step 2?
6. **Wildcards** (`*.example.com:443`) in the baked list: they make the row wider than a list of hosts. Offer them, or exact
   hosts only?
7. **Classification** as `irreversible-consequential` for `GET`: right, or `read-only` with a warning?
8. **Name of the repository and of the binary** (`cancho-fetch`, `fetch`): the MCP tool is `fetch`.

## 8.1 Step 1, as built and measured

`scripts/fetch_build.py --host http://127.0.0.1:18080 --host http://127.0.0.1:18081 [--allow-private]` writes
`generated/fetch/fetch.cho` (one `via_i` per host, each holding the `Out("host:port")` its `narrow` produced) and runs the
manifest gate. `tests/conformance/test_fetch.py` runs two builds against loopback servers: 23 tests, all passing.

* **The row is derived, and equals the hosts.** For the two hosts above the pinned compiler (597e6b0) reports
  `args, clock, conn_read, conn_write, err_write, heap, io_write, net_out("127.0.0.1:18080"), net_out("127.0.0.1:18081"), poll`,
  `bounded: true`, no `net_in`, no `ffi`, no `file_*`, `dir_*` or `exec`. `scripts/manifest.py` compares the `net_out`
  arguments with the hosts in `generated/fetch/fetch.cho` and fails on any difference, so adding a host in the source
  without going through the build input is a gate failure. **Claim confirmed.**
* **`conn_peer` works on a client connection**, and the peer is in the envelope (`data.peer`). A build without
  `--allow-private` refuses a loopback peer with `fetch.peer-not-public` after connecting and before sending a byte
  (the test checks the server received nothing). The check is on the address the connection actually has, so it holds
  for a name that resolves somewhere private. **Claim confirmed for IPv4**; the IPv6 ranges are now in the check too (`::/96`, mapped IPv4 and NAT64 by
  their embedded address, `fc00::/7`, `fe80::/10`, `ff00::/8`, `2001:db8::/32`), held by `tests/peer_test.cho` (`cancho test`),
  because this environment has no IPv6 socket to dial; an end-to-end IPv6 dial is therefore **not** measured.
* **The deadline holds after the name is resolved**: a server that accepts and goes silent is cut at the deadline
  (`--timeout-ms 500` answered in 503 ms), in the head and in the body. **The resolver caveat of §5.5 is not measured**:
  a resolver that never answers needs a network namespace this environment does not have, so the claim stays as written
  (not enforceable during `getaddrinfo`) and the tests say nothing about it.
* **Framing**: `Content-Length`, chunked, and close-delimited bodies, the cap on each (a 5,000-byte body under a 100-byte cap is
  `fetch.body-too-large` for all three; a body of exactly 100 is accepted), `HEAD` without a body, truncated and non-HTTP answers.
* **A defect the tests found**: `valid_extra` of the shared wire module accepts a *run* of header lines, so an `--accept`
  holding CRLF passed as two headers (`Accept: x` and `X-Evil: 1`). `fetch` now refuses a value with a control byte
  (`fetch.header-malformed`) before the wire module sees it; `test_header_injection_is_refused` holds the case. The
  wire module's `valid_extra` is correct for its own callers (they pass lines, not values); the lesson is that a
  caller building a line from a value must check the value.
* **Not done in step 1**: CI (the pinned compiler has to be built at 597e6b0 first, minutes of work per run, to be
  cached), the MCP server, a build-time refusal of an `https://` entry (the generator rejects
  anything but `http://host[:port]`).

## 8.2 Step 3 as built: `post`

`fetch post PATH --body TEXT --content-type TYPE`. Decisions, each with its reason:

* **`--body`, and later `--body-stdin`.** `--body` carries the request bodies an agent writes (JSON, a form), up to **65,536 bytes**
  (`fetch.request-body-too-large` beyond). `--body-stdin` was added afterwards (8.8) for a caller that does not want to quote a
  body into one argument; the cost was `io_read` in every build's authority row, which this section first refused to pay.
* **`--content-type` is required with `--body`** (`fetch.body-required` names both). A default type would be a guess that
  is right for one server and silently wrong for the next; the error's hint gives an example, which is what a caller
  needs to repair it.
* **`get` and `head` with `--body` are refused** (`fetch.body-not-allowed`), not ignored: a body that was never sent is a
  surprise nobody asked for.
* The body is sent after the head, under the same deadline; framing is `Content-Length` (the body is whole in memory,
  capped), never chunked. Header injection through `--content-type` is refused the same way as through `--accept`.
* The authority row is unchanged by `post` (the same `net_out` hosts, no `io_read`). `introspect`'s reversibility stays
  `irreversible-consequential` for the tool as a whole; `get`/`head` are idempotent and `post` is not, which the MCP
  annotations (step 4) will say per method.

## 8.3 Step 4 as built: the MCP server

`server/mcp.cho` (copied from cancho-table's, itself a copy of cancho-tools'; the third copy, so the drift risk the
cancho-table doc names is now three-fold and the shared-module split it recommends is more worth doing), `scripts/mcp.py`
(the generator: `generated/mcp/tools.cho` from `fetch introspect`), `server/ceiling.toml`, `manifests/mcp.authority.json`,
and `tests/conformance/test_mcp.py` (10 tests, through the real server and the real `fetch` against loopback).

* **Changes from the cancho-table copy**: no `--root` (the tool reads no file, and its hosts are baked, not chosen by the
  server); `openWorldHint` is derived from `net_out` in the tool's row (true here, false for a tool without it);
  the `stdin` property and `mcp.input-*` rules stay in the code (generic) and are inert, because `fetch` reads no standard
  input and `introspect` says so.
* **Annotations, derived from `fetch introspect`**: `readOnlyHint` false, `destructiveHint` true, `idempotentHint` false,
  `openWorldHint` true. They are per tool, so `get` shares `post`'s conservative answer. A per-method annotation needs
  two tools (`fetch_get`, `fetch_post`), which the generator does not do; not built.
* **The server's own row** has no `net_out`, `net_in`, `ffi` or file label, only `exec` narrowed to the binary directory
  (checked by a test): the network authority stays in the tool, whose row lists the hosts.
* **Found: the MCP server's output bound ended as `mcp.signaled` as often as `mcp.output-too-large`.** The same call (a
  100,000-byte body, `--max-output 2000`). **This note first blamed the copied server or `std.process.capture_both`; that was
  wrong, and is corrected here.** It was `fetch` itself: the SIGILL trap on a response whose first read held more than 8 KiB
  of body (8.7), which the server saw as "the tool was ended by a signal". Measured later, 40 runs each on this machine: the
  build before that fix answers `mcp.signaled` 40 times, the current build `mcp.output-too-large` 40 times, and
  `capture_both` at the pinned revision is the same source as at cancho's main. `test_output_bound` now asserts the one rule.
* **Found in `fetch`**: a `;` in a flag's help text splits a row of the flag table, so `introspect` listed garbage flags
  (this showed up only because the generator reads them). Fixed, and `test_introspect_flags_are_well_formed` holds it.
* **Built later (8.9)**: the `enum` of baked hosts in the `host` property. **Not built**: a `--with fetch` entry in a combined server.

## 8.4 Step 5 as built: redirects

`--max-redirects N` (0 to 5, default 0). With 0 a 3xx is the answer, as before. With N, a 301, 302, 303, 307 or 308 with a
`Location` is followed up to N times, and an N+1th is `fetch.redirect-limit`.

* **Only to a host the build was given, by a target checked before a socket opens.** A `Location` is an absolute path on
  the same host, or `http://AUTHORITY/path` where AUTHORITY is one of the baked entries (`h` and `h:80` are one; host
  names compare case-insensitively; the port otherwise exactly). Everything else is `fetch.redirect-not-allowed` with
  `detail.location` and `detail.reason`: another host, `https://`, a relative path, a network-path (`//h/x`), userinfo
  (`http://baked@evil/` has the authority `baked@evil`, which is no entry), a path the tool would not send. The test sends
  seven such locations and checks that the other baked host's server received nothing. No "follow, then check".
* **Method**: 301, 302 and 303 turn a `post` into a `get` without its body or `Content-Type` (what browsers do, and
  recorded in the hop); 307 and 308 keep the method and the body. `head` stays `head`.
* **Deadline**: `--timeout-ms` is for the chain; each hop starts with what is left (never less than 1 ms).
* **Envelope**: `data.redirects` is always present (empty without a hop), each hop `{status, from, path, to, to_path,
  method}`; `host`, `path`, `method` and `status` describe the last request. Fragments are dropped from a `Location`.
* **Where the loop is**: the generated main (each baked host has its own `Out`, so the next hop's capability is picked by
  index); `exchange` decides the target and returns it, `retarget` rebuilds the plan. The authority row did not change.
* **Not built**: a redirect to an `https://` baked host (no HTTPS yet); (credentials, 8.6: the rule is that each hop uses its own host's credential, so none follows a redirect).

## 8.5 Step 2 as built: `https`, on the pure TLS client, with the roots in the binary

`fetch_build.py --host https://h[:443] --roots ca.pem`. The decision you made (pure TLS, baked roots) and what it costs:

* **The client is `packages/tls` and `packages/x509` at the pinned revision (597e6b0), unreviewed.** `docs/ROADMAP.md` #281
  says its independent review (#209) has not happened and that a full handshake costs 4 to 7 times OpenSSL's CPU. Nothing here
  makes either better; `fetch` over `https` is therefore **not to be described as production-ready**, and the README says so.
* **The trust roots are a build input, not a file read at run time**: `--roots FILE` is embedded as a literal, so the row has
  no `fs_read` of a roots path, a changed root is a rebuild, and a missing `--roots` with an `https://` host is a build
  error. The chain is always checked against the name as baked (`localhost` in the tests); there is no flag that turns
  verification off.
* **The row gains one label, `fs_read("/dev/urandom")`**, for the 32-byte seed of the engine's generator, and only when an
  `https://` host is baked: `scripts/manifest.py` fails if the label appears without one (`tools.toml`: `seed = "https-only"`).
  Everything else is as before: the TLS engine takes no capability.
* **Refusals**: one rule, `fetch.tls`, with `detail.tag` from the engine (`tls-...`, `x509-...`). The tests hold: a
  certificate for another name, a chain from an authority that is not trusted, a plain HTTP server answering on an `https`
  port, a silent server cut at the deadline. In each, nothing of the request reaches the server.
* **Redirects across schemes** are followed only to a baked entry of the same scheme as the `Location` says
  (`http://h` never reaches an `https` entry, or the reverse); `h:443` and `h` are one authority for `https`.
* **`data.scheme`** is `http` or `https`. A close-delimited body over TLS needs the peer's `close_notify`, as it should.
* **Measured: the compile.** The build now compiles 17 more modules and takes about 2.5 minutes (the `install`/`manifest`
  steps do not, the `build` does), and the suite builds twice, so it takes about 10 minutes. `tests/peer_test.cho` was
  9 minutes before the address ranges moved to their own module (`fetchpeer`), and is 0.2 s now.
* **Not done**: ALPN and resumption are off (not asked for); the memory of a handshake is not measured here (the engine
  is opened with one work area); a host in `*.suffix` form cannot be `https` yet (the name check is against the baked name).

## 8.6 Credentials: a file per https host, the secret never in the caller's hands

`fetch_build.py --host https://api.example.com --credential api.example.com=/run/secrets/api_token`
(`HOST=PATH[,Header]`; the header defaults to `Authorization`, sent as `Bearer <token>`; with a header named, the token is
the raw value, e.g. `...=/run/secrets/key,X-Api-Key`).

* **The channel is a file, and the row says which.** The binary gains `fs_read("/run/secrets/api_token")` per credentialed
  host, nothing else; `scripts/manifest.py` fails if a `fs_read` is not `/dev/urandom` (with an https host) or a declared
  credential path, if a declared path is not in the row, or if a credential is for a host that is not a baked `https://` one
  (`tools.toml`). The operator mounts the secret (a container secret, a tmpfs file); the agent never has it, never names it,
  and cannot ask for another: there is no flag that selects a credential or a header.
* **Only over https, and only to its own host.** `--credential` for an `http://` entry is a build error. The credential is
  looked up per request by the baked host index, so a redirect to another host (which is itself a baked entry) uses that
  host's credential or none: it never follows. Tested with a redirect from a credentialed https host to a plain one (the
  second server sees no token) and to a second credentialed one (it sees its own, not the first's).
* **Read when that host is dialed, before the connection.** A file that is missing, empty, or not a token (visible ASCII, no
  blank or control byte) is `fetch.credential-unavailable` with `detail.path`: exit 8, and the test checks the server saw
  nothing. A trailing newline is dropped. The secret is never in an error, the envelope, `introspect` or `skill`; the envelope
  has `data.credentialed` (true or false) so a caller knows without being told the value.
* **Erasure is best effort.** The token is read into a region array, copied into the request's header area (also a region
  array, not a heap buffer, so it can be cleared), and both are zeroed after the request is written. cancho has no guaranteed
  erasure (`docs/tls-pure.md` 7.3), and the TLS engine holds the plaintext record it sealed; so a process dump during a request
  can contain the token. The mitigation is the usual one: a short-lived process, one request.
* **Not built, deliberately:** a credential for a wildcard host, several credentials for one host, a token refreshed at run
  time, cookies, and any credential over plain http.

## 8.7 fetch against curl, measured

`python3 scripts/bench.py` (curl 8.5.0 with OpenSSL 3.0.13; Linux x86-64, 4 cores, a shared machine; one process per request,
output discarded, medians of 40 runs for 1 KiB, 15 for 1 MiB and 7 for 16 MB; the server is a Python threaded server in
another process holding the body in memory; the TLS certificates are ECDSA P-256). `scripts/maxrss.c` launches each process,
because a child forked from Python inherits Python's 24 MB high-water mark (the first version of this benchmark measured the
harness, not the tools). Times are wall milliseconds, memory the peak resident set in KiB.

| body | | curl | fetch (json) | fetch (text) |
|---|---|---|---|---|
| 1 KiB | http | 7.4 ms, 10,816 KiB | **2.0 ms**, 2,684 KiB | 2.0 ms, 2,686 KiB |
| 1 KiB | https | 9.2 ms, 12,820 KiB | **3.6 ms**, 2,924 KiB | 4.0 ms, 2,932 KiB |
| 1 MiB | http | 6.8 ms, 10,896 KiB | 7.5 ms, 6,572 KiB | **2.8 ms**, 3,628 KiB |
| 1 MiB | https | 12.7 ms, 12,780 KiB | 9.6 ms, 6,820 KiB | **5.0 ms**, 4,004 KiB |
| 16 MB | http | **14.1 ms**, 10,944 KiB | 76.0 ms, 34,760 KiB | 16.2 ms, 18,176 KiB |
| 16 MB | https | **21.1 ms**, 12,808 KiB | 91.7 ms, 34,992 KiB | 28.1 ms, 18,596 KiB |

(Second round, after the change below; the first round's numbers were 1 MiB http 8.2 ms and 16 MB http 100 ms / 50 MB in
json, 104 ms / 43 MB in text.)

* **For one small request `fetch` is about three times faster and uses a quarter of the memory.** The difference is not the
  protocol; it is that curl loads its libraries and initialises them (about 6 ms of CPU to fetch 1 KiB over http), and `fetch` is
  one static executable. For an agent that makes many small requests, one process each, that is the number that matters.
* **Text mode was doing the JSON envelope's work.** `callgrind` on a 1 MiB body showed 27.0M instructions, most of them
  `json.put_string` (about 15 an input byte) and the UTF-8 check, which ran in `--format text` although text mode prints
  the body as it is. Text mode now skips both: 0.99M instructions, 1 MiB http 7.8 ms to 2.8 ms, 16 MB 104 ms to 16 ms (level
  with curl at 1 MiB, within 1.2 times at 16 MB). Bodies of known length or read-to-close are also read straight into the body
  buffer, reserved up front, instead of through the 8 KiB staging buffer; chunked bodies still go through `absorb`.
* **In json mode a 16 MB body is five times slower than curl** (76 ms; 35 MB of memory, down from 50 MB). `callgrind`: 301 M
  instructions, 170 M in the string escaper of `std.json` and 96 M (6 instructions a byte) in `toolbox.text.is_ascii`, the byte loop that `text.put` runs over the whole body to decide between a string and a base64 object before `std.json` escapes it (so two passes over every byte: 6 and about 10 instructions). `fetch` cannot replace the
  escaper: the only way to hand `std.json` pre-escaped text is `put_fragment`, which parses and validates what it is given.
  The escaper was made faster in cancho (#589, 15 to about 10 instructions a byte on text with spaces); built with that
  compiler the json 16 MB row measured 81 ms against 76 ms (medians of 7 on a busy machine), inside the run-to-run spread, so
  the tool stays on its pin (597e6b0) and this row is not claimed to have improved. What is left is those two passes. Unrolling `is_ascii` four bytes at a time measured 0.13 instructions a byte better (not kept); the real saving is one pass instead of two, which needs `std.json` to say whether a string it wrote was pure ASCII (or `text.put` to write the string itself), and is at most a third of the 16 MB json row's CPU. Not made here; it changes a library API for a body size outside what the tool is for.
  The memory is bounded by
  the cap (the ceiling is 16 MiB), but "bounded" is not "small". A body of that size is outside what the tool is for (an
  answer an agent can read), and the cap says so.
* **TLS costs `fetch` little here**: a handshake adds about 1.5 ms to a 1 KiB request (3.6 ms against 2.0 ms), and about 12 ms of CPU over plain http for 16 MB (text mode).
  The bytes are right (16,000,000 over TLS with the same MD5 as the source). **I cannot explain the 16 MB figure from the engine's
  documented throughput** (`docs/tls-performance.md` in cancho; ChaCha20 at 135 MB/s would cost ~120 ms), and did not profile it.
  The tables above use P-256 ECDSA certificates. `bench.py --chain rsa2048|ec384` measures the other two (an authority and a
  server certificate of that key type; one run each, a loaded shared machine, so read them as orders of magnitude):

  | https, 1 KiB | curl | fetch |
  |---|---|---|
  | P-256 ECDSA | 9.2 ms | 3.6 ms |
  | RSA-2048 | 10.1 ms | **3.3 ms** |
  | P-384 ECDSA | 12.2 ms | **5.6 ms** |

  So the handshake does not get dear with RSA-2048 (verifying a signature with a public exponent of 65537 is cheap) and P-384
  adds about 2 ms; `fetch` stays about two to three times faster than curl for a small request on all three. The 1 MiB rows of
  these runs (RSA 11.0 ms, P-384 14.7 ms for `fetch` text) are slower than the P-256 table's 5.0 ms by more than the 1 KiB
  rows are, which a handshake cannot explain; the machine was busier (curl's own 1 MiB row moved from 12.7 to 10.5 and 12.3
  ms), and I did not repeat them. A chain with an intermediate, which public sites serve, was not measured. The ROADMAP's "4 to 7
  times OpenSSL's CPU for a handshake" is therefore neither confirmed nor contradicted by this benchmark: a whole process is
  3 ms against 9 ms because curl's start-up dwarfs both handshakes.
* **What the benchmark found in `fetch`**: a trap (SIGILL) on any response whose first read held more than 8 KiB of body, which
  failed every 1 MiB and 16 MB run of the first attempt (8.1 fixed it and `test_a_body_larger_than_the_first_read_buffers`
  holds it). No earlier test had a body over 100 KB.
* **What the benchmark found in the benchmark**: the 45 ms that every https request seemed to cost was the test server's
  Nagle/delayed-ACK interaction (it is gone with `TCP_NODELAY`; `BENCH_NODELAY=0` brings it back), identical for both tools.

Chunked bodies were measured once against a 16 MB body of the same bytes (4 KiB and 64 KiB chunks, text mode, http): 26 to 30 ms of CPU
against 22 ms for the same body with a Content-Length, so chunk decoding costs about 0.3 ns a byte and needs no fast path.

Not measured: many requests on one connection (the tool does one), IPv6, a slow or distant server, a server that answers with
`Content-Encoding` (the tool asks for `identity`), and anything on a machine that is not this one.

## 8.8 `--body-stdin`

`fetch post PATH --body-stdin --content-type TYPE < body`. Decisions:

* **The authority row gains `io_read`** (standard input), for every build: the row is derived from the code and the code can read
  it, so the row says so (`tools.toml` allows it with a comment; `introspect` says `stdin: "with --body-stdin"`). Nothing else
  changed in the row: still no file, no `net_in`, no `ffi`, and the hosts are the same.
* **Same ceiling as `--body`: 65,536 bytes.** One byte more is read to tell "exactly the ceiling" from "more"; more is
  `fetch.request-body-too-large` (flag `--body-stdin`) and no socket is opened. An empty standard input is an empty body
  (`Content-Length: 0`), as `--body ""` is.
* **Read before any socket opens**, so a refusal costs the network nothing and a slow writer on standard input is not under the
  request's deadline. (A caller that never closes standard input blocks the tool; the caller owns that pipe.)
* **`--body` and `--body-stdin` together are `args.conflict`**; `--body-stdin` with `get` or `head` is `fetch.body-not-allowed`,
  and without `--content-type` `fetch.body-required`, as for `--body`.
* **The MCP server does not offer it.** It has no way to feed the tool's standard input and the `body` property carries the same
  bytes, so `scripts/mcp.py` withholds the flag (and, as before, still refuses any other tool that reads standard input without an
  operand to replace). `tools/list` has no `stdin` property for `fetch`.
* **Tests**: a body with a non-ASCII byte arrives whole with the right `Content-Length`; exactly 65,536 passes and 65,537 is
  refused with no request; empty is empty; the three rule cases above.

## 8.9 The hosts in the MCP schema

`tools/list` now gives `host` an `enum` of the build's hosts, so an agent is told what it may name instead of finding out from a
refusal. **The tool did not change**: the hosts are already in `introspect`'s authority row (`net_out("host:port")`), which
`scripts/mcp.py` reads when it writes `generated/mcp/tools.cho`. (The first note on this said `introspect` would have to list
the hosts; it already did, in the row.)

* **Advisory.** The server checks types, not membership; a host outside the list still reaches the tool and is refused there as
  `fetch.host-not-allowed` with the list in `detail`. A caller that names a baked host without its port where the port is the
  scheme's default is accepted by the tool and is not in the `enum`; the schema lists the exact entries, which always work.
* **Per build.** The enum is the hosts baked into the build the server was generated against, so the committed
  `generated/mcp/tools.cho` carries the two canonical test hosts, and a real deployment regenerates it with its own
  (`scripts/mcp.py build`), as it already had to for the tool's directory.
* **Test**: `test_tools_list` compares the enum to the hosts the test build was given.
