# cancho-fetch

An agent-safe `fetch`: HTTP and HTTPS requests to a **fixed, built-in list of hosts**, with that list in the binary's
authority row (`net_out("api.example.com:443")`) where a reviewer can read it, instead of in a flag a caller can set.
Written in [cancho](https://github.com/alpibrusl/cancho), following the contract of
[cancho-tools](https://github.com/alpibrusl/cancho-tools).

**Status: design only. Nothing is built.** The design is [docs/design.md](docs/design.md) (it is being reviewed in
the open; the first pull request carries it). The landing page is [site/index.html](site/index.html), published with
GitHub Pages once Pages is switched to "GitHub Actions" in the repository settings.

## What it will be

```
fetch get  /v1/items --host api.example.com --max-bytes 65536
fetch post /v1/items --host api.example.com --body-stdin
```

One binary per allow-list. The hosts are `narrow` literals in the source, so `cancho authority` shows one `net_out` label
per host and nothing else reaches the network: no `ffi`, no `net_in`, no host that is not in the list. A request for any
other host is refused before a socket opens, with a rule tag and the list in `data`.

## Why a separate repository

`cancho-tools` sells "none of these tools can reach the network" as a checked property. A tool that holds `net_out`
would make that "except one", so the network tier lives here with its own ceiling file and its own review, and reuses
the same envelope and contract so a host can treat the two as one family.

## What it depends on

* cancho's exact host matching (`edition 10`, [alpibrusl/cancho#561](https://github.com/alpibrusl/cancho/pull/561)); until it
  is merged the host half of a bound is a prefix and the central claim is not true of the compiler.
* `packages/tls`, `packages/x509` and `packages/http-client` in cancho; the TLS stack's own status is "not independently
  reviewed (#209)", which the design says out loud.

## Not here

`ssh`, other HTTP methods, proxies, cookies, HTTP/2, client certificates. See the design, section 3 and section 8.

## Licence

Not chosen yet; `cancho-tools` is EUPL-1.2. See the open question in the pull request.
