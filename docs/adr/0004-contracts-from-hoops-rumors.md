---
status: accepted
---

# Contracts come from Hoops Rumors' non-guaranteed lists, not Spotrac

The project spec took contracts from Spotrac's team cap pages, with RealGM as the fallback. The terms check
NEU-1873 required (2026-10-23) ruled both out. Spotrac's Terms of Service forbid "any spider,
robot, cheat utility, scraper, or offline reader", "systematically extract[ing] … any data or data
fields" and incorporating its content "in any data base or other compilation". RealGM's Terms of Use require
"click-by-click selections directly by a human being, unaided by a computer". Both answer a
non-browser client with 403 (Spotrac's "browser isn't supported" page, RealGM's Cloudflare
challenge), so the scraper would also have to impersonate a browser to get past protection put
there to stop it. The other sources with contract data are out too:
- ESPN: Disney's terms forbid "robot, spider, script … web scraping".
- HoopsHype: Spotrac's owner; it answers a plain client with 402.
- gabriel1200/site_Data on GitHub: unlicensed, and itself a Spotrac/RealGM scrape via curl_cffi.

Hoops Rumors maintains, each season until January 10, a team-by-team list of every player without
a fully guaranteed salary and a list of early salary-guarantee dates. That is exactly what
`contract_pressure` (spec §7.7) needs. Its robots.txt allows the articles (`Crawl-delay: 1`), it
publishes no terms of use, and plain httpx is served. So `contract` holds non-guaranteed contracts
only: two Hoops Rumors pages fetched weekly, from URLs committed per season.

## Consequences

- `contract` narrows: no salaries of fully guaranteed players, no options, no trade-eligible dates.
  A player absent from a week's snapshot is taken as fully guaranteed. Spec §8's "contract notes" on
  the team page can show only non-guaranteed deals.
- Two-way status is `roster_membership`'s alone, which the M4 milestone already made the single source of
  contract-type badges; `contract.two_way` is dropped.
- There is no fallback source. A failed week leaves the previous snapshot in place and pings the
  weekly heartbeat's `/fail`.
- The article URLs change every season and must be added to `sources/hoopsrumors.py` by hand
  (May–July); a season without them fails loudly rather than silently writing nothing.
- An absence of published terms is not a grant. If Hoops Rumors publishes terms that forbid this,
  the overlay goes behind `HOOPSRUMORS_ENABLED=false` and this ADR is revisited.
