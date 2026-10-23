# closinglineup

A private NBA dashboard about each player's place in his team's rotation on a given date, and the minutes, production and impact trends that explain changes in it. Minutes lead; box production lags.

## Language

### Rotation

**Rotation state**:
A player's rotation tier on one team on one game date, together with the inputs that produced it.
_Avoid_: role, status (status is for injuries)

**Rotation tier**:
One of Injured, Starter, Core, Fringe, Out, resolved in that order of precedence. Computed per team per date from the role window's minutes share, starter rate and the current availability; never hand-assigned.
_Avoid_: role, depth-chart position, two-way (a badge, not a tier)

**Badge**:
A contract or assignment fact shown beside a tier: two-way, 10-day, Exhibit 10, G League assignment. Badges explain a tier; they never replace one.

**Rotation event**:
A change of rotation tier between two consecutive team game dates, with the before and after tiers and the top contributing signals.
_Avoid_: transition (fine in prose, but the entity is an event)

**Closer**:
A player on the floor in the final five minutes of a game whose margin was within ten points at that moment.

**Closing lineup**:
The five players on the floor at the final buzzer of a game decided by ten points or fewer; undefined for other games.

**Minutes share**:
A player's minutes divided by his team's total available player-minutes over the games the team played in the window (240 per regulation game, plus overtime).

### Time

**Game date**:
The Eastern-time calendar date a game is scheduled on. Every snapshot, window and rotation state is keyed by game date, never by wall-clock date.

**Window**:
A trailing span measured in a team's games (5, 10, 20), or the whole season, or the whole prior season. Windows count team games played, so a player's DNPs and inactives are inside the window.
_Avoid_: period (reserved for quarters and overtime)

**Role window**:
A window over a player's games with his current team only. Minutes share, starter and closer rate, rotation rank, tier and flags are role-window metrics; they restart when a player changes team.

**Production window**:
A window over a player's games regardless of team. Efficiency, usage, shot profile and impact are production-window metrics; season and prior-season baselines are production windows.

**Short window**:
A window that holds fewer games than its nominal length because the player's role window began recently (a trade, signing or call-up). Shown as short, never padded.

**Season**:
An NBA season labelled by both years, "2025-26". Regular season and play-in/playoffs are distinct phases.

### Play-by-play

**Stint**:
A continuous span within one period during which the same ten players were on the floor, with its possessions and points for and against from each team's view.
_Avoid_: lineup segment, shift

**Lineup**:
A set of five teammates on the floor together, identified by their sorted player IDs. Lineup results are sums over stints. Reconciliation alone credits possessions as NBA.com does: each one to every lineup on the floor for any of it, so one a substitution splits counts for both (NEU-1899), and at a substitution logged at the instant one starts or ends, to the lineups on the floor for the events there (NEU-1900). These are its **possession lineups**.

**Possession**:
One team's offensive possession as pbpstats emits it, with how it started, how it ended and the score margin.

**Garbage time**:
Play after the game is decided by a fixed margin-and-clock rule; excluded from on-court ratings and on/off by default.

### Overlays

**Availability**:
Whether a player can play on a date: active, injury status (Out / Doubtful / Questionable / Probable / Available), inactive, suspended, or assigned to the G League.
_Avoid_: health, injury (an injury is the reason, availability is the state)

**Transaction**:
A roster move on a date: sign, waive, trade, two-way conversion, G League assign or recall, option decision.

**Impact snapshot**:
One public (EPM, DARKO) or locally computed (BPM) impact metric value for a player on a date, with its offensive and defensive split and its minutes basis.
_Avoid_: rating (reserved for on-court ORtg/DRtg/NetRtg)

**Flag**:
A named boolean condition on a player-team-date derived from windows and overlays (for example minutes cliff, earning trust, yo-yo), raised so the morning page can surface it.
_Avoid_: alert
