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
  `User-Agent`, `X-*`). `Authorization`, `Cookie`, `Proxy-*` and `Host` are `fetch.header-forbidden` in v1 (open
  question 2: credentials).
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
2. **Credentials.** v1 forbids `Authorization` and `Cookie`. An agent that needs an API key has no way in. Options: a header
   whose value is read from an environment variable named at build time (the row would show no new label, since env comes in
   `args`), a baked token (worse), or "not in v1". Which?
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
  for a name that resolves somewhere private. **Claim confirmed for IPv4**; the IPv6 ranges (`::1`, `fc00::/7`, `fe80::/10`,
  mapped IPv4) are not in the check yet and are a step-2 item.
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
  cached), the MCP server, IPv6 peer ranges, a build-time refusal of an `https://` entry (the generator rejects
  anything but `http://host[:port]`).

