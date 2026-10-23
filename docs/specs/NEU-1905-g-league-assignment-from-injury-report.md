# NEU-1905: G League assignment from the official injury report

Linear: [NEU-1905](https://linear.app/neuroticsasquatch/issue/NEU-1905) · Story NEU-1844 · Milestone M4 Overlays
Related: NEU-1869 (transactions, PR #33, merged), NEU-1867 (injury PDF, merged), NEU-1876 (rotation tiers, backlog)

## What and why

`current_g_league_assignment(session, player_id, day)` in `backend/src/closinglineup/derive/roster.py`
reads `assign` / `recall` rows from `transaction`, and nothing writes any: NBA.com's
`NBA_Player_Movement.json` carries only signings, claims, waivers, trades and conversions (checked
2026-10-23, 10,118 rows), and no G League feed URL answers. So the helper is False for every
player, and the `g_league` badge and the "Out, including a current G League assignment" rule of
spec §7.5 can never fire.

The official injury report already carries the signal. Teams list a player on assignment with
reason `G League - On Assignment`; the `injuries` overlay (NEU-1867) stores every line in
`injury_status.reason` with `source='pdf'`, four times a day. On the 2026-03-01 17:15 fixture that
is 8 lines of 134 (plus 33 `G League - Two-Way`). This ticket rewrites the helper to read that.

## Decisions

1. **Source is `injury_status`, `source='pdf'` only.** ESPN fallback rows (`source='espn'`) are
   ignored: their reasons are body parts, not roster status. A team whose newest row is ESPN's
   carries its last PDF-derived state forward.
2. **A line's status does not matter.** A line whose reason is exactly `G League - On Assignment`
   means assigned whether its status is Out, Questionable or Available. The fixture has one of
   each of the last two (Amari Williams BOS, Justin Edwards PHI) and both read True. The Injured
   tier (NEU-1876) still fires only on Out/Doubtful, so an Available line gives a badge, not a
   tier. Compare the reason string exactly as the parser stores it; do not substring-match
   (`G League - Two-Way` must not match).
3. **The team is the team of the player's latest PDF line.** The helper keeps its signature. It
   does not consult `roster_membership`.
4. **Recall is absence from a newer report that covers the team.** A report lists every
   unavailable player of each team whose game it covers, so a newer report with any row for the
   team that does not list him means he is available, hence recalled.
5. **"Covers the team" means "has at least one `injury_status` row for the team".** The table
   stores no row for a team with no unavailable players or a `NOT YET SUBMITTED` team, so such a
   report cannot be told from one that did not cover the team, and the player's state carries over
   from the last report that has a row for the team. Accepted: an NBA team with no line at all is
   rare, and a two-way or injured teammate nearly always supplies one.
6. **Transaction `assign` / `recall` rows keep precedence on their date.** The classifier keeps
   classifying those types (NEU-1869) in case the feed ever carries them. If a row exists, it
   decides whenever its `date` is on or after the Eastern date of the PDF evidence the helper would
   otherwise use (a tie goes to the transaction). With no such rows, which is today's state, the
   PDF rule alone decides.
7. **No box-score change.** `parse/box.py` keeps mapping both `G League - On Assignment` and
   `G League - Two-Way` to `dnp_reason='g_league'`. Deferred, see below.

## The rule

For `(player_id, day)`, with `end` = midnight Eastern at the start of `day + 1` (as
`derive.availability.latest_injury_source` computes it):

1. **Latest line.** The player's `injury_status` row with `source='pdf'`, `game_date <= day` and
   `report_datetime < end`, newest `report_datetime` first. None: not assigned (then step 4).
2. **Assigned?** Its `reason == 'G League - On Assignment'`. Otherwise not assigned (step 4).
3. **Recalled since?** Any `injury_status` row with `source='pdf'`, `team_id` equal to the line's,
   `game_date <= day`, `report_datetime` strictly greater than the line's and `< end`. One exists:
   not assigned. None: assigned.
4. **Transaction override.** The player's newest `transaction` row of type `assign` or `recall`
   with `date <= day` (same ordering as today: date desc, recall before assign on a tie). If it
   exists and its `date >= ` the Eastern date of the line found in step 1 (or there is no line),
   the answer is `type == 'assign'`. Otherwise the answer from steps 1–3 stands.

Consequences the rule gives, which the tests pin:

- A day the team does not play carries the last covering report's state over (step 1 picks the
  last game's lines; step 3 finds nothing newer).
- A later edition of the same game's report (17:15 after 13:00) that drops him reads as recalled.
- A line for tomorrow's game (`game_date > day`) is not read today, matching §7.5's "at or before
  the date".
- An ESPN row, however new, is invisible to every step.

`derive/availability.py` is not changed; NEU-1876 calls the helper from there.

## Files

- `backend/src/closinglineup/derive/roster.py`: rewrite `current_g_league_assignment`; drop the
  `Transaction`-only query; module docstring's last paragraph names the injury report as the
  source and the transaction override. Import `InjuryStatus`, `PDF_SOURCE` (from
  `closinglineup.injuries`) and `EASTERN` (from `sources.injury_pdf`) as `derive/availability.py`
  does. Name the reason string once as a module constant, e.g. `ON_ASSIGNMENT = "G League - On Assignment"`.
- `backend/tests/test_derive_roster.py`: replace `test_an_assignment_holds_until_the_recall` with
  the tests below (the transaction-only case becomes the override test).
- `docs/specs/closinglineup-v1-rotation-and-value-dashboard-project-spec.md`: §4.4's last sentence
  and §7.5's badge sentence name the injury report as the assignment source, replacing the
  "no source yet (NEU-1869)" note.
- `CONTEXT.md`: Availability entry already updated to name the source.

## Acceptance criteria

All are `db`-fixture tests (they skip without `DATABASE_URL`), using player IDs no real player has
and dates no real report has, cleared before and after, as `test_availability.py` and
`test_derive_roster.py` do.

- [ ] **Fixture, assigned:** the 2026-03-01T1715 fixture stored through the `injuries` overlay
      path (`tests/test_injuries.py`'s `seeded` fixture and `players.json` resolve its names):
      each of the 8 players with a `G League - On Assignment` line reads True for 2026-03-01,
      including the Questionable and Available ones; a player with a `G League - Two-Way` line
      reads False; a player with an injury line reads False.
- [ ] **Assign then recall:** a player on assignment in his team's day-1 report and absent from
      its day-3 report (which lists a teammate) reads True on day 1 and day 2 (no game), False on
      day 3 and day 4.
- [ ] **Same-game later edition:** on assignment in the 13:00 report for a game, absent from the
      17:15 report for the same game, reads False on that day.
- [ ] **Tomorrow's line is not today's:** a line for `day + 1` does not make him assigned on `day`.
- [ ] **ESPN ignored:** a team whose newest row is `source='espn'` (newer than its last PDF report)
      carries the player's last PDF-derived state forward, both when that state is assigned and
      when it is recalled.
- [ ] **Transaction override:** an `assign` transaction dated after his last PDF line (which did
      not list him assigned) reads True; a `recall` dated the same day as a PDF line listing him
      assigned reads False; a transaction dated before the PDF line does not override it.
- [ ] `derive/roster.py`'s docstring and spec §4.4 / §7.5 name the injury report as the source.
- [ ] `uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest`
      pass from `backend/`.

## Out of scope / deferred

- The `g_league` badge and the Out tier themselves (NEU-1876).
- Splitting the box-score DNP mapping into distinct `dnp_reason` values for On Assignment and
  Two-Way (new enum value, migration, reparse). Deferred; the injury report is the only source.
- Using `G League - Two-Way` lines to confirm `roster_membership.contract_type`.
- Any change to the movement-feed overlay or the classifier.
- A report with no row for a team (healthy team, `NOT YET SUBMITTED`) counting as coverage; see
  decision 5.

References: project spec §4.3, §4.4, §7.5; NEU-1869 (PR #33); NEU-1867 (PR #31).
