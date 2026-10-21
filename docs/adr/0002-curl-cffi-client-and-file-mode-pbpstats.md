---
status: accepted
---

# One curl_cffi client for NBA.com; pbpstats only ever reads cached files

`nba_api` is blocked as shipped: Akamai fingerprints the TLS handshake, and from the home IP on 2026-10-20 requests, httpx and curl all got a 403 from stats.nba.com and cdn.nba.com, while curl_cffi imitating Chrome with nba.com `Origin`/`Referer` headers got a 200 (nba-today ADR-0004, `src/nba_today/sources/nba_http.py`). So the fetcher has its own thin client, ported from nba-today: curl_cffi, `impersonate="chrome"`, the nba.com headers, ~1 request/second with jitter, and retries on connection errors and 5xx. It calls stats.nba.com and cdn.nba.com endpoints directly, using `nba_api`'s endpoint documentation as the parameter reference. `nba_api` is not a runtime dependency. pbpstats does its own HTTP with `requests`, which is equally blocked, so the fetcher writes raw responses into pbpstats's data directory layout and pbpstats runs in file mode: it never makes a network call.

## Considered options

- **Monkeypatch `nba_api`'s session with curl_cffi.** Keeps its endpoint classes and DataFrame parsing, but relies on an undocumented internal that breaks on upgrade.
- **Probe `nba_api` from kumadrome first.** The nba-today probe already answered this from the same IP.

## Consequences

- Endpoint parameter names and response column order are pinned by recorded fixtures per endpoint, since there is no library between us and NBA.com to absorb drift.
- The raw cache is the contract between fetching and parsing: every parser (ours and pbpstats) is re-runnable from disk without the network.
