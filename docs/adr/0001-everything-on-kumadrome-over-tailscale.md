---
status: accepted
---

# Everything runs on kumadrome, reached over the personal tailnet

stats.nba.com drops connections from datacenter IP ranges and fingerprints TLS, so the fetch tier has to run from a residential IP. kumadrome (the home Mac mini, always on, Docker, residential IP) is the only host that fits. Rather than fetch at home and ship data to the Hetzner/Coolify VM for serving, the database, API and UI also run on kumadrome in one compose project, and the UI is reached through a userspace Tailscale sidecar on the personal tailnet, the same pattern as Navidrome and the personal Uptime Kuma. This is a single-user private tool: zero hosting cost, no public surface, no sync pipeline to keep healthy.

## Considered options

- **B. Fetch at home, serve from Coolify.** Reachable without Tailscale, but adds an rsync/object-push step that can silently stall, and the Coolify host is designated for neuroticsasquat.ch tools rather than personal apps. Rejected for v1; the fetcher's raw cache and the parquet interchange keep the door open.

## Consequences

- The host itself joins no tailnet; only the UI sidecar does (`TS_HOSTNAME`, never a container hostname matching the service it proxies).
- Backups of the database are kumadrome's responsibility (a systemd timer like the existing Kuma and Navidrome backups).
