-- player_window_stats: every spec §7.1–7.3 metric per player, game date and window (spec §6.4).
--
-- A materialized view, refreshed after each nightly load (ADR-0003): a metric's definition
-- changes in one migration, never in stored rows. `derive.windows.refresh_windows` refreshes it.
-- This file is the definition; the migration that last created the view holds a copy of it, and
-- `tests/test_windows.py` keeps the two the same.
--
-- Rows. One per (player_id, team_id, game_date, window), where window is one of 5, 10, 20, season
-- and prior_season, and game_date is a date the player's team played a final game while he was on
-- its roster (`roster_membership`), DNPs, inactives and absences from the box score included:
--
-- * Role rows (team_id set; windows 5, 10, 20, season) carry the §7.1 and §7.3 metrics, over the
--   player's games with that team only. A role window restarts when he joins a team, or rejoins
--   it after a gap in his roster spans; spans that run one into the next, as a new contract on the
--   same team does, are one spell (CONTEXT.md, "Role window").
-- * Production rows (team_id NULL; every window) carry the §7.2 metrics, Game Score (v2 spec
--   §8.1), the latest impact snapshots and the latest value snapshots, Win Shares and VORP (v2
--   spec §8.4), over the player's games whatever his team (CONTEXT.md, "Production window").
--
-- Windows count team games, not games played: a 5-game window at date d is his team's last five
-- games through d, d included (spec §7). Every window but prior_season stays within d's season, so
-- the first game of a season starts every window afresh. A window holding fewer games than its
-- nominal length computes on what it has and sets `is_short` (CONTEXT.md, "Short window"); the
-- season and prior_season windows have no nominal length and are never short. A player with no
-- games in the prior season has no prior_season row.
--
-- Deltas. Every windowed metric has `<metric>_delta_prior`, its value less its value over the
-- equal-length window immediately before (games 6–10 for the 5-game window at game 10), and
-- `<metric>_delta_season`, its value less the season window's at the same date (spec §7). A prior
-- window computes on what it has too, and is absent, its deltas NULL, before the player's
-- (n + 1)th team game. A production row's season window has the prior season as its prior window;
-- a role row's season window, and prior_season, have none. The games and possession counts and
-- the impact and value snapshots carry no deltas.
--
-- Not here yet: §7.3's net rating of the player's three most-used 2-man combos, and on-court
-- ratings with garbage time included, which the API's `garbage_time=include` will need.
--
-- Ratios are of sums over the window, never averages of per-game rates; a ratio whose denominator
-- is zero is NULL. Rates are fractions, as `player_game`'s are (0.222 for 22.2%); ratings are per
-- 100 possessions.
--
-- How it is computed: each spell's and each player-season's games are numbered 1, 2, ... in date
-- order and their stats summed cumulatively. A window's sums are the running total less the one n
-- games back, and its prior window's value is its own value n games back, each a `lag` over the
-- games in order: no window joins its own rows, so the plan does not hang on the planner's
-- estimate of how many there are.

CREATE MATERIALIZED VIEW player_window_stats AS
WITH
-- Window lengths in team games; NULL is the whole season to date.
window_def (window_name, n) AS (
    VALUES ('5', 5), ('10', 10), ('20', 20), ('season', NULL::int)
),

-- §7 windows, keyed by the team's game dates: both teams' view of every final game. A team plays
-- one game a date; were the schedule to hold two, the first by game ID counts, so the view's key
-- stays unique and its refresh cannot fail on it.
team_game AS (
    SELECT DISTINCT ON (team_id, game_date) *
    FROM (
        SELECT game_id, season, game_date, home_team_id AS team_id, away_team_id AS opp_id
        FROM game
        WHERE status = 'final'
        UNION ALL
        SELECT game_id, season, game_date, away_team_id, home_team_id
        FROM game
        WHERE status = 'final'
    ) both_sides
    ORDER BY team_id, game_date, game_id
),

-- A player's spells on a team: his roster spans there, merged where one starts no later than the
-- day after every earlier one has ended. An open span runs to infinity.
span AS (
    SELECT
        player_id,
        team_id,
        start_date,
        coalesce(end_date, 'infinity'::date) AS end_date,
        CASE
            WHEN start_date <= max(coalesce(end_date, 'infinity'::date)) OVER (
                PARTITION BY player_id, team_id
                ORDER BY start_date
                ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
            ) + 1 THEN 0
            ELSE 1
        END AS opens_spell
    FROM roster_membership
),
spell AS (
    SELECT player_id, team_id, spell_no, min(start_date) AS start_date, max(end_date) AS end_date
    FROM (
        SELECT
            *,
            sum(opens_spell) OVER (PARTITION BY player_id, team_id ORDER BY start_date) AS spell_no
        FROM span
    ) numbered
    GROUP BY player_id, team_id, spell_no
),

-- Each team's box-score totals in a game. Its available player-seconds are five players over the
-- game's length, 48 minutes plus 5 for each overtime, the overtimes read off the players' seconds,
-- which sum to five times the length give or take rounding (§7.1).
team_box AS (
    SELECT
        game_id,
        team_id,
        sum(seconds) AS seconds,
        sum(fgm) AS fgm,
        sum(fga) AS fga,
        sum(fta) AS fta,
        sum(tov) AS tov,
        sum(oreb) AS oreb,
        sum(dreb) AS dreb,
        (5 * (2880 + 300 * round(greatest(sum(seconds) / 5.0 - 2880, 0) / 300)))::int
            AS available_seconds
    FROM player_game
    GROUP BY game_id, team_id
),

-- Each team's stints with their defensive possessions (the other team's view of the same stint),
-- the margin at their start (the team's points for less against over its earlier stints, as
-- `derive.garbage_time` reads it) and the game's last period.
stint_context AS (
    SELECT
        s.game_id,
        s.team_id,
        s.lineup_key,
        s.possessions,
        coalesce(o.possessions, 0) AS opp_possessions,
        s.pts_for,
        s.pts_against,
        -- §7.3: on-court ratings and on/off are garbage-time filtered; an unclassified stint
        -- counts.
        s.is_garbage_time IS NOT TRUE AS counts,
        -- §7.1 closer: a stint of the game's final five minutes (the last period's, which is all
        -- of an overtime) that starts within 10, read as closer than 10 as `derive.garbage_time`
        -- reads it. The margin is the one at the stint's start, the nearest the stints carry to
        -- the margin at the moment; one starting before the five-minute mark is judged by it.
        s.period = max(s.period) OVER (PARTITION BY s.game_id)
            AND s.end_sec > CASE WHEN s.period <= 4 THEN 720 ELSE 300 END - 300
            AND abs(coalesce(sum(s.pts_for - s.pts_against) OVER (
                PARTITION BY s.game_id, s.team_id
                ORDER BY s.period, s.start_sec
                ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
            ), 0)) < 10 AS closing
    FROM stint s
    LEFT JOIN stint o
        ON o.game_id = s.game_id
        AND o.period = s.period
        AND o.start_sec = s.start_sec
        AND o.team_id <> s.team_id
),
-- A team's garbage-filtered possessions and points in a game, and whether it had a closing stint.
team_stints AS (
    SELECT
        game_id,
        team_id,
        bool_or(closing) AS closing,
        coalesce(sum(possessions) FILTER (WHERE counts), 0) AS poss,
        coalesce(sum(opp_possessions) FILTER (WHERE counts), 0) AS opp_poss,
        coalesce(sum(pts_for) FILTER (WHERE counts), 0) AS pts_for,
        coalesce(sum(pts_against) FILTER (WHERE counts), 0) AS pts_against
    FROM stint_context
    GROUP BY game_id, team_id
),
-- The same for each player over the stints he was on the floor for, plus his possessions
-- unfiltered, which the per-100 rates divide by since the box score's totals include garbage time.
player_stints AS (
    SELECT
        game_id,
        team_id,
        member::int AS player_id,
        bool_or(closing) AS closed,
        sum(possessions) AS all_poss,
        coalesce(sum(possessions) FILTER (WHERE counts), 0) AS poss,
        coalesce(sum(opp_possessions) FILTER (WHERE counts), 0) AS opp_poss,
        coalesce(sum(pts_for) FILTER (WHERE counts), 0) AS pts_for,
        coalesce(sum(pts_against) FILTER (WHERE counts), 0) AS pts_against
    FROM stint_context
    CROSS JOIN LATERAL unnest(string_to_array(lineup_key, '-')) AS member
    GROUP BY game_id, team_id, member
),
-- A player's field-goal attempts and makes in a game by zone, from his shot chart (§7.2).
player_shots AS (
    SELECT
        game_id,
        player_id,
        count(*) FILTER (WHERE zone = 'rim') AS rim_att,
        count(*) FILTER (WHERE zone = 'rim' AND made) AS rim_made,
        count(*) FILTER (WHERE zone = 'short_mid') AS short_mid_att,
        count(*) FILTER (WHERE zone = 'short_mid' AND made) AS short_mid_made,
        count(*) FILTER (WHERE zone = 'long_mid') AS long_mid_att,
        count(*) FILTER (WHERE zone = 'long_mid' AND made) AS long_mid_made,
        count(*) FILTER (WHERE zone = 'corner_3') AS corner_3_att,
        count(*) FILTER (WHERE zone = 'corner_3' AND made) AS corner_3_made,
        count(*) FILTER (WHERE zone = 'above_break_3') AS above_break_3_att,
        count(*) FILTER (WHERE zone = 'above_break_3' AND made) AS above_break_3_made
    FROM shot
    GROUP BY game_id, player_id
),

-- Every team game of every spell, numbered within the spell's season: the role windows' games.
role_game AS (
    SELECT
        sp.player_id,
        sp.team_id,
        sp.spell_no,
        tg.season,
        tg.game_id,
        tg.game_date,
        tg.opp_id,
        row_number() OVER (
            PARTITION BY sp.player_id, sp.team_id, sp.spell_no, tg.season ORDER BY tg.game_date
        ) AS k
    FROM spell sp
    JOIN team_game tg
        ON tg.team_id = sp.team_id
        AND tg.game_date BETWEEN sp.start_date AND sp.end_date
),

-- §7.1 and §7.3 per role game. He played if he has seconds; a game he did not play is a DNP-CD if
-- the box says coach's decision, and otherwise inactive: injured, suspended, away, assigned, or
-- missing from the box, which lists only active players. So played + dnp_cd + inactive is the
-- window's games.
role_fact AS (
    SELECT
        rg.player_id,
        rg.team_id,
        rg.spell_no,
        rg.season,
        rg.game_date,
        rg.k,
        (coalesce(pg.seconds, 0) > 0)::int AS played,
        (coalesce(pg.seconds, 0) > 0 AND pg.is_starter)::int AS started,
        (coalesce(pg.seconds, 0) = 0 AND coalesce(pg.dnp_reason = 'cd', false))::int AS dnp_cd,
        (coalesce(pg.seconds, 0) = 0 AND pg.dnp_reason IS DISTINCT FROM 'cd')::int AS inactive,
        coalesce(pg.seconds, 0) AS seconds,
        (coalesce(pg.seconds, 0) / 60.0) ^ 2 AS minutes_sq,
        coalesce(tb.available_seconds, 5 * 2880) AS team_seconds,
        coalesce(ts.closing, false)::int AS closing_game,
        coalesce(ps.closed, false)::int AS closed_game,
        coalesce(ps.poss, 0) AS on_poss,
        coalesce(ps.opp_poss, 0) AS on_opp_poss,
        coalesce(ps.pts_for, 0) AS on_pts_for,
        coalesce(ps.pts_against, 0) AS on_pts_against,
        coalesce(ts.poss, 0) AS team_poss,
        coalesce(ts.opp_poss, 0) AS team_opp_poss,
        coalesce(ts.pts_for, 0) AS team_pts_for,
        coalesce(ts.pts_against, 0) AS team_pts_against
    FROM role_game rg
    LEFT JOIN player_game pg
        ON pg.game_id = rg.game_id AND pg.player_id = rg.player_id AND pg.team_id = rg.team_id
    LEFT JOIN team_box tb ON tb.game_id = rg.game_id AND tb.team_id = rg.team_id
    LEFT JOIN team_stints ts ON ts.game_id = rg.game_id AND ts.team_id = rg.team_id
    LEFT JOIN player_stints ps
        ON ps.game_id = rg.game_id AND ps.team_id = rg.team_id AND ps.player_id = rg.player_id
),
role_cum AS (
    SELECT
        player_id,
        team_id,
        spell_no,
        season,
        game_date,
        k,
        sum(played) OVER spell_to_date AS played,
        sum(started) OVER spell_to_date AS started,
        sum(dnp_cd) OVER spell_to_date AS dnp_cd,
        sum(inactive) OVER spell_to_date AS inactive,
        sum(seconds) OVER spell_to_date AS seconds,
        sum(minutes_sq) OVER spell_to_date AS minutes_sq,
        sum(team_seconds) OVER spell_to_date AS team_seconds,
        sum(closing_game) OVER spell_to_date AS closing_games,
        sum(closed_game) OVER spell_to_date AS closed_games,
        sum(on_poss) OVER spell_to_date AS on_poss,
        sum(on_opp_poss) OVER spell_to_date AS on_opp_poss,
        sum(on_pts_for) OVER spell_to_date AS on_pts_for,
        sum(on_pts_against) OVER spell_to_date AS on_pts_against,
        sum(team_poss) OVER spell_to_date AS team_poss,
        sum(team_opp_poss) OVER spell_to_date AS team_opp_poss,
        sum(team_pts_for) OVER spell_to_date AS team_pts_for,
        sum(team_pts_against) OVER spell_to_date AS team_pts_against
    FROM role_fact
    WINDOW spell_to_date AS (
        PARTITION BY player_id, team_id, spell_no, season ORDER BY k ROWS UNBOUNDED PRECEDING
    )
),
-- Each role window's sums: the running totals less those n games back, none before the spell's
-- first game; the season window's are the running totals themselves.
role_window AS (
    SELECT
        c.player_id,
        c.team_id,
        c.spell_no,
        c.season,
        c.k,
        c.game_date,
        w.window_name,
        w.n,
        least(c.k, coalesce(w.n, c.k)) AS games,
        c.played - coalesce(lag(c.played, w.n) OVER back, 0) AS played,
        c.started - coalesce(lag(c.started, w.n) OVER back, 0) AS started,
        c.dnp_cd - coalesce(lag(c.dnp_cd, w.n) OVER back, 0) AS dnp_cd,
        c.inactive - coalesce(lag(c.inactive, w.n) OVER back, 0) AS inactive,
        (c.seconds - coalesce(lag(c.seconds, w.n) OVER back, 0))::float8 AS seconds,
        (c.minutes_sq - coalesce(lag(c.minutes_sq, w.n) OVER back, 0))::float8 AS minutes_sq,
        (c.team_seconds - coalesce(lag(c.team_seconds, w.n) OVER back, 0))::float8 AS team_seconds,
        c.closing_games - coalesce(lag(c.closing_games, w.n) OVER back, 0) AS closing_games,
        c.closed_games - coalesce(lag(c.closed_games, w.n) OVER back, 0) AS closed_games,
        c.on_poss - coalesce(lag(c.on_poss, w.n) OVER back, 0) AS on_poss,
        c.on_opp_poss - coalesce(lag(c.on_opp_poss, w.n) OVER back, 0) AS on_opp_poss,
        (c.on_pts_for - coalesce(lag(c.on_pts_for, w.n) OVER back, 0))::float8 AS on_pts_for,
        (c.on_pts_against - coalesce(lag(c.on_pts_against, w.n) OVER back, 0))::float8
            AS on_pts_against,
        c.team_poss - coalesce(lag(c.team_poss, w.n) OVER back, 0) AS team_poss,
        c.team_opp_poss - coalesce(lag(c.team_opp_poss, w.n) OVER back, 0) AS team_opp_poss,
        (c.team_pts_for - coalesce(lag(c.team_pts_for, w.n) OVER back, 0))::float8 AS team_pts_for,
        (c.team_pts_against - coalesce(lag(c.team_pts_against, w.n) OVER back, 0))::float8
            AS team_pts_against
    FROM role_cum c
    CROSS JOIN window_def w
    WINDOW back AS (
        PARTITION BY c.player_id, c.team_id, c.spell_no, c.season, w.window_name ORDER BY c.k
    )
),
role_metric AS (
    SELECT
        *,
        -- §7.1 rotation rank: his place on the team by minutes share over the window, 1 the most.
        rank() OVER (
            PARTITION BY team_id, game_date, window_name ORDER BY minutes_share DESC NULLS LAST
        ) AS rotation_rank
    FROM (
        SELECT
            player_id,
            team_id,
            spell_no,
            season,
            k,
            game_date,
            window_name,
            n,
            games,
            played,
            started,
            -- §7.1 minutes per game played.
            seconds / 60.0 / nullif(played, 0) AS minutes_per_game,
            -- §7.1 minutes share: his seconds over his team's available player-seconds in the
            -- window's team games, overtime included.
            seconds::float8 / nullif(team_seconds, 0) AS minutes_share,
            -- §7.1 minutes volatility: the sample standard deviation of his minutes over the
            -- games he played, from the sums of minutes and of their squares.
            CASE
                WHEN played >= 2 THEN sqrt(greatest(
                    (minutes_sq - (seconds / 60.0) ^ 2 / played) / (played - 1), 0
                ))
            END AS minutes_volatility,
            -- §7.1 starter rate: games started over games played.
            started::float8 / nullif(played, 0) AS starter_rate,
            -- §7.1 closer rate: games he was on the floor for a closing stint over team games
            -- that had one.
            closed_games::float8 / nullif(closing_games, 0) AS closer_rate,
            -- §7.1 DNP-CD and inactive counts.
            dnp_cd AS dnp_cd_count,
            inactive AS inactive_count,
            -- §7.3 on-court ratings, garbage time excluded: offense over the team's possessions
            -- with him on the floor, defense over the opponent's.
            100.0 * on_pts_for / nullif(on_poss, 0) AS on_court_ortg,
            100.0 * on_pts_against / nullif(on_opp_poss, 0) AS on_court_drtg,
            100.0 * on_pts_for / nullif(on_poss, 0)
                - 100.0 * on_pts_against / nullif(on_opp_poss, 0) AS on_court_net,
            -- §7.3 on/off: his on-court net rating less the team's with him off the floor, and
            -- the possessions each rests on.
            100.0 * on_pts_for / nullif(on_poss, 0)
                - 100.0 * on_pts_against / nullif(on_opp_poss, 0)
                - (
                    100.0 * (team_pts_for - on_pts_for) / nullif(team_poss - on_poss, 0)
                    - 100.0 * (team_pts_against - on_pts_against)
                        / nullif(team_opp_poss - on_opp_poss, 0)
                ) AS on_off_net,
            on_poss AS on_court_poss,
            on_opp_poss AS on_court_opp_poss,
            team_poss - on_poss AS off_court_poss,
            team_opp_poss - on_opp_poss AS off_court_opp_poss
        FROM role_window
    ) metric
),
-- The prior window's value is the window's own value n games back in the spell; the season's,
-- the season window's at the same game.
role_row AS (
    SELECT
        player_id,
        team_id,
        game_date,
        window_name,
        games AS games_in_window,
        games < coalesce(n, 0) AS is_short,
        played AS games_played,
        started AS games_started,
        minutes_per_game,
        minutes_per_game - lag(minutes_per_game, n) OVER back AS minutes_per_game_delta_prior,
        minutes_per_game - max(minutes_per_game) FILTER (WHERE window_name = 'season') OVER anchor
            AS minutes_per_game_delta_season,
        minutes_share,
        minutes_share - lag(minutes_share, n) OVER back AS minutes_share_delta_prior,
        minutes_share - max(minutes_share) FILTER (WHERE window_name = 'season') OVER anchor
            AS minutes_share_delta_season,
        minutes_volatility,
        minutes_volatility - lag(minutes_volatility, n) OVER back AS minutes_volatility_delta_prior,
        minutes_volatility - max(minutes_volatility)
            FILTER (WHERE window_name = 'season') OVER anchor
            AS minutes_volatility_delta_season,
        starter_rate,
        starter_rate - lag(starter_rate, n) OVER back AS starter_rate_delta_prior,
        starter_rate - max(starter_rate) FILTER (WHERE window_name = 'season') OVER anchor
            AS starter_rate_delta_season,
        closer_rate,
        closer_rate - lag(closer_rate, n) OVER back AS closer_rate_delta_prior,
        closer_rate - max(closer_rate) FILTER (WHERE window_name = 'season') OVER anchor
            AS closer_rate_delta_season,
        rotation_rank,
        rotation_rank - lag(rotation_rank, n) OVER back AS rotation_rank_delta_prior,
        rotation_rank - max(rotation_rank) FILTER (WHERE window_name = 'season') OVER anchor
            AS rotation_rank_delta_season,
        dnp_cd_count,
        dnp_cd_count - lag(dnp_cd_count, n) OVER back AS dnp_cd_count_delta_prior,
        dnp_cd_count - max(dnp_cd_count) FILTER (WHERE window_name = 'season') OVER anchor
            AS dnp_cd_count_delta_season,
        inactive_count,
        inactive_count - lag(inactive_count, n) OVER back AS inactive_count_delta_prior,
        inactive_count - max(inactive_count) FILTER (WHERE window_name = 'season') OVER anchor
            AS inactive_count_delta_season,
        on_court_ortg,
        on_court_ortg - lag(on_court_ortg, n) OVER back AS on_court_ortg_delta_prior,
        on_court_ortg - max(on_court_ortg) FILTER (WHERE window_name = 'season') OVER anchor
            AS on_court_ortg_delta_season,
        on_court_drtg,
        on_court_drtg - lag(on_court_drtg, n) OVER back AS on_court_drtg_delta_prior,
        on_court_drtg - max(on_court_drtg) FILTER (WHERE window_name = 'season') OVER anchor
            AS on_court_drtg_delta_season,
        on_court_net,
        on_court_net - lag(on_court_net, n) OVER back AS on_court_net_delta_prior,
        on_court_net - max(on_court_net) FILTER (WHERE window_name = 'season') OVER anchor
            AS on_court_net_delta_season,
        on_off_net,
        on_off_net - lag(on_off_net, n) OVER back AS on_off_net_delta_prior,
        on_off_net - max(on_off_net) FILTER (WHERE window_name = 'season') OVER anchor
            AS on_off_net_delta_season,
        on_court_poss,
        on_court_opp_poss,
        off_court_poss,
        off_court_opp_poss
    FROM role_metric
    WINDOW
        back AS (PARTITION BY player_id, team_id, spell_no, season, window_name ORDER BY k),
        anchor AS (PARTITION BY player_id, team_id, spell_no, season, k)
),

-- A player's team games across his spells, one a date (the one he has a box line for, should two
-- spells' teams both play), numbered within the season: the production windows' games.
prod_game AS (
    SELECT
        player_id,
        team_id,
        season,
        game_id,
        game_date,
        opp_id,
        row_number() OVER (PARTITION BY player_id, season ORDER BY game_date) AS k
    FROM (
        SELECT DISTINCT ON (rg.player_id, rg.game_date) rg.*
        FROM role_game rg
        LEFT JOIN player_game pg
            ON pg.game_id = rg.game_id AND pg.player_id = rg.player_id AND pg.team_id = rg.team_id
        ORDER BY rg.player_id, rg.game_date, pg.player_id IS NULL, rg.team_id
    ) one_a_date
),
-- §7.2 per production game: his box line, and his team's and the opponent's totals in the games
-- he played, which the rate formulas weigh his minutes against (Basketball Reference's glossary,
-- https://www.basketball-reference.com/about/glossary.html).
prod_fact AS (
    SELECT
        g.player_id,
        g.season,
        g.game_date,
        g.k,
        (coalesce(pg.seconds, 0) > 0)::int AS played,
        coalesce(pg.seconds, 0) AS seconds,
        coalesce(pg.pts, 0) AS pts,
        coalesce(pg.fgm, 0) AS fgm,
        coalesce(pg.fga, 0) AS fga,
        coalesce(pg.fg3m, 0) AS fg3m,
        coalesce(pg.fg3a, 0) AS fg3a,
        coalesce(pg.fta, 0) AS fta,
        coalesce(pg.ftm, 0) AS ftm,
        coalesce(pg.oreb, 0) AS oreb,
        coalesce(pg.dreb, 0) AS dreb,
        coalesce(pg.ast, 0) AS ast,
        coalesce(pg.stl, 0) AS stl,
        coalesce(pg.blk, 0) AS blk,
        coalesce(pg.tov, 0) AS tov,
        coalesce(pg.pf, 0) AS pf,
        CASE WHEN pg.seconds > 0 THEN tb.seconds ELSE 0 END AS team_seconds,
        CASE WHEN pg.seconds > 0 THEN tb.fgm ELSE 0 END AS team_fgm,
        CASE WHEN pg.seconds > 0 THEN tb.fga ELSE 0 END AS team_fga,
        CASE WHEN pg.seconds > 0 THEN tb.fta ELSE 0 END AS team_fta,
        CASE WHEN pg.seconds > 0 THEN tb.tov ELSE 0 END AS team_tov,
        CASE WHEN pg.seconds > 0 THEN tb.oreb ELSE 0 END AS team_oreb,
        CASE WHEN pg.seconds > 0 THEN tb.dreb ELSE 0 END AS team_dreb,
        CASE WHEN pg.seconds > 0 THEN coalesce(ob.oreb, 0) ELSE 0 END AS opp_oreb,
        CASE WHEN pg.seconds > 0 THEN coalesce(ob.dreb, 0) ELSE 0 END AS opp_dreb,
        coalesce(ps.all_poss, 0) AS on_poss,
        coalesce(sh.rim_att, 0) AS rim_att,
        coalesce(sh.rim_made, 0) AS rim_made,
        coalesce(sh.short_mid_att, 0) AS short_mid_att,
        coalesce(sh.short_mid_made, 0) AS short_mid_made,
        coalesce(sh.long_mid_att, 0) AS long_mid_att,
        coalesce(sh.long_mid_made, 0) AS long_mid_made,
        coalesce(sh.corner_3_att, 0) AS corner_3_att,
        coalesce(sh.corner_3_made, 0) AS corner_3_made,
        coalesce(sh.above_break_3_att, 0) AS above_break_3_att,
        coalesce(sh.above_break_3_made, 0) AS above_break_3_made
    FROM prod_game g
    LEFT JOIN player_game pg
        ON pg.game_id = g.game_id AND pg.player_id = g.player_id AND pg.team_id = g.team_id
    LEFT JOIN team_box tb ON tb.game_id = g.game_id AND tb.team_id = g.team_id
    LEFT JOIN team_box ob ON ob.game_id = g.game_id AND ob.team_id = g.opp_id
    LEFT JOIN player_stints ps
        ON ps.game_id = g.game_id AND ps.team_id = g.team_id AND ps.player_id = g.player_id
    LEFT JOIN player_shots sh ON sh.game_id = g.game_id AND sh.player_id = g.player_id
),
prod_cum AS (
    SELECT
        player_id,
        season,
        game_date,
        k,
        sum(played) OVER season_to_date AS played,
        sum(seconds) OVER season_to_date AS seconds,
        sum(pts) OVER season_to_date AS pts,
        sum(fgm) OVER season_to_date AS fgm,
        sum(fga) OVER season_to_date AS fga,
        sum(fg3m) OVER season_to_date AS fg3m,
        sum(fg3a) OVER season_to_date AS fg3a,
        sum(fta) OVER season_to_date AS fta,
        sum(ftm) OVER season_to_date AS ftm,
        sum(oreb) OVER season_to_date AS oreb,
        sum(dreb) OVER season_to_date AS dreb,
        sum(ast) OVER season_to_date AS ast,
        sum(stl) OVER season_to_date AS stl,
        sum(blk) OVER season_to_date AS blk,
        sum(tov) OVER season_to_date AS tov,
        sum(pf) OVER season_to_date AS pf,
        sum(team_seconds) OVER season_to_date AS team_seconds,
        sum(team_fgm) OVER season_to_date AS team_fgm,
        sum(team_fga) OVER season_to_date AS team_fga,
        sum(team_fta) OVER season_to_date AS team_fta,
        sum(team_tov) OVER season_to_date AS team_tov,
        sum(team_oreb) OVER season_to_date AS team_oreb,
        sum(team_dreb) OVER season_to_date AS team_dreb,
        sum(opp_oreb) OVER season_to_date AS opp_oreb,
        sum(opp_dreb) OVER season_to_date AS opp_dreb,
        sum(on_poss) OVER season_to_date AS on_poss,
        sum(rim_att) OVER season_to_date AS rim_att,
        sum(rim_made) OVER season_to_date AS rim_made,
        sum(short_mid_att) OVER season_to_date AS short_mid_att,
        sum(short_mid_made) OVER season_to_date AS short_mid_made,
        sum(long_mid_att) OVER season_to_date AS long_mid_att,
        sum(long_mid_made) OVER season_to_date AS long_mid_made,
        sum(corner_3_att) OVER season_to_date AS corner_3_att,
        sum(corner_3_made) OVER season_to_date AS corner_3_made,
        sum(above_break_3_att) OVER season_to_date AS above_break_3_att,
        sum(above_break_3_made) OVER season_to_date AS above_break_3_made
    FROM prod_fact
    WINDOW season_to_date AS (PARTITION BY player_id, season ORDER BY k ROWS UNBOUNDED PRECEDING)
),
-- Each production window's sums, as for role windows.
prod_window AS (
    SELECT
        c.player_id,
        c.season,
        c.k,
        c.game_date,
        w.window_name,
        w.n,
        least(c.k, coalesce(w.n, c.k)) AS games,
        (c.played - coalesce(lag(c.played, w.n) OVER back, 0))::float8 AS played,
        (c.seconds - coalesce(lag(c.seconds, w.n) OVER back, 0))::float8 AS seconds,
        (c.pts - coalesce(lag(c.pts, w.n) OVER back, 0))::float8 AS pts,
        (c.fgm - coalesce(lag(c.fgm, w.n) OVER back, 0))::float8 AS fgm,
        (c.fga - coalesce(lag(c.fga, w.n) OVER back, 0))::float8 AS fga,
        (c.fg3m - coalesce(lag(c.fg3m, w.n) OVER back, 0))::float8 AS fg3m,
        (c.fg3a - coalesce(lag(c.fg3a, w.n) OVER back, 0))::float8 AS fg3a,
        (c.fta - coalesce(lag(c.fta, w.n) OVER back, 0))::float8 AS fta,
        (c.ftm - coalesce(lag(c.ftm, w.n) OVER back, 0))::float8 AS ftm,
        (c.oreb - coalesce(lag(c.oreb, w.n) OVER back, 0))::float8 AS oreb,
        (c.dreb - coalesce(lag(c.dreb, w.n) OVER back, 0))::float8 AS dreb,
        (c.ast - coalesce(lag(c.ast, w.n) OVER back, 0))::float8 AS ast,
        (c.stl - coalesce(lag(c.stl, w.n) OVER back, 0))::float8 AS stl,
        (c.blk - coalesce(lag(c.blk, w.n) OVER back, 0))::float8 AS blk,
        (c.tov - coalesce(lag(c.tov, w.n) OVER back, 0))::float8 AS tov,
        (c.pf - coalesce(lag(c.pf, w.n) OVER back, 0))::float8 AS pf,
        (c.team_seconds - coalesce(lag(c.team_seconds, w.n) OVER back, 0))::float8 AS team_seconds,
        (c.team_fgm - coalesce(lag(c.team_fgm, w.n) OVER back, 0))::float8 AS team_fgm,
        (c.team_fga - coalesce(lag(c.team_fga, w.n) OVER back, 0))::float8 AS team_fga,
        (c.team_fta - coalesce(lag(c.team_fta, w.n) OVER back, 0))::float8 AS team_fta,
        (c.team_tov - coalesce(lag(c.team_tov, w.n) OVER back, 0))::float8 AS team_tov,
        (c.team_oreb - coalesce(lag(c.team_oreb, w.n) OVER back, 0))::float8 AS team_oreb,
        (c.team_dreb - coalesce(lag(c.team_dreb, w.n) OVER back, 0))::float8 AS team_dreb,
        (c.opp_oreb - coalesce(lag(c.opp_oreb, w.n) OVER back, 0))::float8 AS opp_oreb,
        (c.opp_dreb - coalesce(lag(c.opp_dreb, w.n) OVER back, 0))::float8 AS opp_dreb,
        (c.on_poss - coalesce(lag(c.on_poss, w.n) OVER back, 0))::float8 AS on_poss,
        (c.rim_att - coalesce(lag(c.rim_att, w.n) OVER back, 0))::float8 AS rim_att,
        (c.rim_made - coalesce(lag(c.rim_made, w.n) OVER back, 0))::float8 AS rim_made,
        (c.short_mid_att - coalesce(lag(c.short_mid_att, w.n) OVER back, 0))::float8
            AS short_mid_att,
        (c.short_mid_made - coalesce(lag(c.short_mid_made, w.n) OVER back, 0))::float8
            AS short_mid_made,
        (c.long_mid_att - coalesce(lag(c.long_mid_att, w.n) OVER back, 0))::float8 AS long_mid_att,
        (c.long_mid_made - coalesce(lag(c.long_mid_made, w.n) OVER back, 0))::float8
            AS long_mid_made,
        (c.corner_3_att - coalesce(lag(c.corner_3_att, w.n) OVER back, 0))::float8 AS corner_3_att,
        (c.corner_3_made - coalesce(lag(c.corner_3_made, w.n) OVER back, 0))::float8
            AS corner_3_made,
        (c.above_break_3_att - coalesce(lag(c.above_break_3_att, w.n) OVER back, 0))::float8
            AS above_break_3_att,
        (c.above_break_3_made - coalesce(lag(c.above_break_3_made, w.n) OVER back, 0))::float8
            AS above_break_3_made
    FROM prod_cum c
    CROSS JOIN window_def w
    WINDOW back AS (PARTITION BY c.player_id, c.season, w.window_name ORDER BY c.k)
),
prod_metric AS (
    SELECT
        player_id,
        season,
        k,
        game_date,
        window_name,
        n,
        games,
        -- §7.2 true shooting: points over twice the true shot attempts, a free throw attempt
        -- 0.44 of one.
        pts / nullif(2 * (fga + 0.44 * fta), 0) AS ts_pct,
        -- §7.2 effective field goal: a made three counts one and a half makes.
        (fgm + 0.5 * fg3m) / nullif(fga, 0) AS efg_pct,
        -- §7.2 usage: his share of the team's plays (attempts, a free throw attempt 0.44 of one,
        -- and turnovers) while he was on the floor.
        (fga + 0.44 * fta + tov) * (team_seconds / 5.0)
            / nullif(seconds * (team_fga + 0.44 * team_fta + team_tov), 0) AS usg_pct,
        -- §7.2 assist rate: his assists over his teammates' field goals while he was on the
        -- floor.
        ast / nullif(seconds / nullif(team_seconds / 5.0, 0) * team_fgm - fgm, 0) AS ast_pct,
        -- §7.2 turnover rate: turnovers per play.
        tov / nullif(fga + 0.44 * fta + tov, 0) AS tov_pct,
        -- §7.2 offensive and defensive rebound rates: his share of the rebounds available while
        -- he was on the floor.
        oreb * (team_seconds / 5.0) / nullif(seconds * (team_oreb + opp_dreb), 0) AS oreb_pct,
        dreb * (team_seconds / 5.0) / nullif(seconds * (team_dreb + opp_oreb), 0) AS dreb_pct,
        -- §7.2 free throw rate and three-point attempt rate, per field goal attempt.
        fta::float8 / nullif(fga, 0) AS ftr,
        fg3a::float8 / nullif(fga, 0) AS fg3a_rate,
        -- §7.2 shot profile: each zone's share of his charted attempts, and his percentage there.
        rim_att::float8 / nullif(
            rim_att + short_mid_att + long_mid_att + corner_3_att + above_break_3_att, 0
        ) AS rim_freq,
        short_mid_att::float8 / nullif(
            rim_att + short_mid_att + long_mid_att + corner_3_att + above_break_3_att, 0
        ) AS short_mid_freq,
        long_mid_att::float8 / nullif(
            rim_att + short_mid_att + long_mid_att + corner_3_att + above_break_3_att, 0
        ) AS long_mid_freq,
        corner_3_att::float8 / nullif(
            rim_att + short_mid_att + long_mid_att + corner_3_att + above_break_3_att, 0
        ) AS corner_3_freq,
        above_break_3_att::float8 / nullif(
            rim_att + short_mid_att + long_mid_att + corner_3_att + above_break_3_att, 0
        ) AS above_break_3_freq,
        rim_made::float8 / nullif(rim_att, 0) AS rim_fg_pct,
        short_mid_made::float8 / nullif(short_mid_att, 0) AS short_mid_fg_pct,
        long_mid_made::float8 / nullif(long_mid_att, 0) AS long_mid_fg_pct,
        corner_3_made::float8 / nullif(corner_3_att, 0) AS corner_3_fg_pct,
        above_break_3_made::float8 / nullif(above_break_3_att, 0) AS above_break_3_fg_pct,
        -- §7.2 per 36 minutes, and per 100 of his team's possessions with him on the floor
        -- (garbage time included, as in the box score).
        pts * 2160.0 / nullif(seconds, 0) AS pts_per36,
        ast * 2160.0 / nullif(seconds, 0) AS ast_per36,
        (oreb + dreb) * 2160.0 / nullif(seconds, 0) AS reb_per36,
        pts * 100.0 / nullif(on_poss, 0) AS pts_per100,
        ast * 100.0 / nullif(on_poss, 0) AS ast_per100,
        (oreb + dreb) * 100.0 / nullif(on_poss, 0) AS reb_per100,
        -- v2 §8.1 Hollinger's Game Score per game played. It is linear in the box line, so the
        -- window's average is the formula over the window's sums.
        (
            pts + 0.4 * fgm - 0.7 * fga - 0.4 * (fta - ftm) + 0.7 * oreb + 0.3 * dreb + stl
            + 0.7 * ast + 0.7 * blk - 0.4 * pf - tov
        ) / nullif(played, 0) AS game_score
    FROM prod_window
),
-- Each player's whole season: his season window at his last team game of it.
prod_season_total AS (
    SELECT DISTINCT ON (player_id, season) *
    FROM prod_metric
    WHERE window_name = 'season'
    ORDER BY player_id, season, k DESC
),
-- The windows, and the prior season's whole at every date of the season after.
prod_all AS (
    SELECT *
    FROM prod_metric
    UNION ALL
    SELECT
        a.player_id,
        a.season,
        a.k,
        a.game_date,
        'prior_season',
        NULL,
        t.games,
        t.ts_pct,
        t.efg_pct,
        t.usg_pct,
        t.ast_pct,
        t.tov_pct,
        t.oreb_pct,
        t.dreb_pct,
        t.ftr,
        t.fg3a_rate,
        t.rim_freq,
        t.short_mid_freq,
        t.long_mid_freq,
        t.corner_3_freq,
        t.above_break_3_freq,
        t.rim_fg_pct,
        t.short_mid_fg_pct,
        t.long_mid_fg_pct,
        t.corner_3_fg_pct,
        t.above_break_3_fg_pct,
        t.pts_per36,
        t.ast_per36,
        t.reb_per36,
        t.pts_per100,
        t.ast_per100,
        t.reb_per100,
        t.game_score
    FROM prod_cum a
    JOIN prod_season_total t
        ON t.player_id = a.player_id
        AND t.season = (left(a.season, 4)::int - 1)::text || '-' || substr(a.season, 3, 2)
),
-- §7.3 the latest impact snapshot of each source at or before each production date, carried
-- forward: each date takes the last snapshot sorted before it, a snapshot on the date included.
impact_asof AS (
    SELECT
        player_id,
        game_date,
        max(value) FILTER (WHERE source = 'epm') AS epm,
        max(o_value) FILTER (WHERE source = 'epm') AS epm_o,
        max(d_value) FILTER (WHERE source = 'epm') AS epm_d,
        max(value) FILTER (WHERE source = 'darko') AS darko,
        max(o_value) FILTER (WHERE source = 'darko') AS darko_o,
        max(d_value) FILTER (WHERE source = 'darko') AS darko_d,
        max(value) FILTER (WHERE source = 'bpm_local') AS bpm_local,
        max(o_value) FILTER (WHERE source = 'bpm_local') AS bpm_local_o,
        max(d_value) FILTER (WHERE source = 'bpm_local') AS bpm_local_d
    FROM (
        SELECT
            player_id,
            source,
            game_date,
            is_date,
            max(value) OVER latest AS value,
            max(o_value) OVER latest AS o_value,
            max(d_value) OVER latest AS d_value
        FROM (
            SELECT
                *,
                count(*) FILTER (WHERE NOT is_date) OVER (
                    PARTITION BY player_id, source ORDER BY game_date, is_date
                ) AS snapshot_no
            FROM (
                SELECT
                    player_id,
                    source,
                    date AS game_date,
                    false AS is_date,
                    value,
                    o_value,
                    d_value
                FROM impact_snapshot
                UNION ALL
                SELECT
                    g.player_id,
                    s.source,
                    g.game_date,
                    true,
                    NULL::float8,
                    NULL::float8,
                    NULL::float8
                FROM prod_game g
                CROSS JOIN (VALUES ('epm'), ('darko'), ('bpm_local')) s (source)
            ) dated
        ) numbered
        WINDOW latest AS (PARTITION BY player_id, source, snapshot_no)
    ) carried
    WHERE is_date
    GROUP BY player_id, game_date
),
-- v2 spec §8.4 the value snapshots, each dated in the season starting that year: a season runs
-- August to July (`season.season_bounds`).
value_snapshot AS (
    SELECT
        player_id,
        source,
        date,
        extract(year FROM date)::int - (extract(month FROM date) < 8)::int AS start_year,
        value,
        o_value,
        d_value
    FROM impact_snapshot
    WHERE source IN ('ws_local', 'vorp_local')
),
-- The latest value snapshot of each source at or before each production date, carried forward as
-- the impact snapshots are but within the date's season: each is the season to date, so the last
-- season's does not carry into this one.
value_asof AS (
    SELECT
        player_id,
        game_date,
        max(value) FILTER (WHERE source = 'ws_local') AS ws_local,
        max(o_value) FILTER (WHERE source = 'ws_local') AS ws_local_o,
        max(d_value) FILTER (WHERE source = 'ws_local') AS ws_local_d,
        max(value) FILTER (WHERE source = 'vorp_local') AS vorp_local
    FROM (
        SELECT
            player_id,
            source,
            game_date,
            is_date,
            max(value) OVER latest AS value,
            max(o_value) OVER latest AS o_value,
            max(d_value) OVER latest AS d_value
        FROM (
            SELECT
                *,
                count(*) FILTER (WHERE NOT is_date) OVER (
                    PARTITION BY player_id, source, start_year ORDER BY game_date, is_date
                ) AS snapshot_no
            FROM (
                SELECT
                    player_id,
                    source,
                    start_year,
                    date AS game_date,
                    false AS is_date,
                    value,
                    o_value,
                    d_value
                FROM value_snapshot
                UNION ALL
                SELECT
                    g.player_id,
                    s.source,
                    left(g.season, 4)::int,
                    g.game_date,
                    true,
                    NULL::float8,
                    NULL::float8,
                    NULL::float8
                FROM prod_game g
                CROSS JOIN (VALUES ('ws_local'), ('vorp_local')) s (source)
            ) dated
        ) numbered
        WINDOW latest AS (PARTITION BY player_id, source, start_year, snapshot_no)
    ) carried
    WHERE is_date
    GROUP BY player_id, game_date
),
-- Each player-season's final value snapshot of each source, which the season after's prior_season
-- rows carry.
value_final AS (
    SELECT
        player_id,
        start_year,
        max(value) FILTER (WHERE source = 'ws_local') AS ws_local,
        max(o_value) FILTER (WHERE source = 'ws_local') AS ws_local_o,
        max(d_value) FILTER (WHERE source = 'ws_local') AS ws_local_d,
        max(value) FILTER (WHERE source = 'vorp_local') AS vorp_local
    FROM (
        SELECT DISTINCT ON (player_id, source, start_year) *
        FROM value_snapshot
        ORDER BY player_id, source, start_year, date DESC
    ) final
    GROUP BY player_id, start_year
),
-- The prior window's value is the window's own value n games back in the season; the season's is
-- the prior season's whole, and the prior season's has none.
prod_row AS (
    SELECT
        m.player_id,
        NULL::int AS team_id,
        m.game_date,
        m.window_name,
        m.games AS games_in_window,
        m.games < coalesce(m.n, 0) AS is_short,
        m.ts_pct,
        m.ts_pct - CASE
            WHEN m.window_name = 'season'
                THEN max(m.ts_pct) FILTER (WHERE m.window_name = 'prior_season') OVER anchor
            ELSE lag(m.ts_pct, m.n) OVER back
        END AS ts_pct_delta_prior,
        m.ts_pct - max(m.ts_pct) FILTER (WHERE m.window_name = 'season') OVER anchor
            AS ts_pct_delta_season,
        m.efg_pct,
        m.efg_pct - CASE
            WHEN m.window_name = 'season'
                THEN max(m.efg_pct) FILTER (WHERE m.window_name = 'prior_season') OVER anchor
            ELSE lag(m.efg_pct, m.n) OVER back
        END AS efg_pct_delta_prior,
        m.efg_pct - max(m.efg_pct) FILTER (WHERE m.window_name = 'season') OVER anchor
            AS efg_pct_delta_season,
        m.usg_pct,
        m.usg_pct - CASE
            WHEN m.window_name = 'season'
                THEN max(m.usg_pct) FILTER (WHERE m.window_name = 'prior_season') OVER anchor
            ELSE lag(m.usg_pct, m.n) OVER back
        END AS usg_pct_delta_prior,
        m.usg_pct - max(m.usg_pct) FILTER (WHERE m.window_name = 'season') OVER anchor
            AS usg_pct_delta_season,
        m.ast_pct,
        m.ast_pct - CASE
            WHEN m.window_name = 'season'
                THEN max(m.ast_pct) FILTER (WHERE m.window_name = 'prior_season') OVER anchor
            ELSE lag(m.ast_pct, m.n) OVER back
        END AS ast_pct_delta_prior,
        m.ast_pct - max(m.ast_pct) FILTER (WHERE m.window_name = 'season') OVER anchor
            AS ast_pct_delta_season,
        m.tov_pct,
        m.tov_pct - CASE
            WHEN m.window_name = 'season'
                THEN max(m.tov_pct) FILTER (WHERE m.window_name = 'prior_season') OVER anchor
            ELSE lag(m.tov_pct, m.n) OVER back
        END AS tov_pct_delta_prior,
        m.tov_pct - max(m.tov_pct) FILTER (WHERE m.window_name = 'season') OVER anchor
            AS tov_pct_delta_season,
        m.oreb_pct,
        m.oreb_pct - CASE
            WHEN m.window_name = 'season'
                THEN max(m.oreb_pct) FILTER (WHERE m.window_name = 'prior_season') OVER anchor
            ELSE lag(m.oreb_pct, m.n) OVER back
        END AS oreb_pct_delta_prior,
        m.oreb_pct - max(m.oreb_pct) FILTER (WHERE m.window_name = 'season') OVER anchor
            AS oreb_pct_delta_season,
        m.dreb_pct,
        m.dreb_pct - CASE
            WHEN m.window_name = 'season'
                THEN max(m.dreb_pct) FILTER (WHERE m.window_name = 'prior_season') OVER anchor
            ELSE lag(m.dreb_pct, m.n) OVER back
        END AS dreb_pct_delta_prior,
        m.dreb_pct - max(m.dreb_pct) FILTER (WHERE m.window_name = 'season') OVER anchor
            AS dreb_pct_delta_season,
        m.ftr,
        m.ftr - CASE
            WHEN m.window_name = 'season'
                THEN max(m.ftr) FILTER (WHERE m.window_name = 'prior_season') OVER anchor
            ELSE lag(m.ftr, m.n) OVER back
        END AS ftr_delta_prior,
        m.ftr - max(m.ftr) FILTER (WHERE m.window_name = 'season') OVER anchor
            AS ftr_delta_season,
        m.fg3a_rate,
        m.fg3a_rate - CASE
            WHEN m.window_name = 'season'
                THEN max(m.fg3a_rate) FILTER (WHERE m.window_name = 'prior_season') OVER anchor
            ELSE lag(m.fg3a_rate, m.n) OVER back
        END AS fg3a_rate_delta_prior,
        m.fg3a_rate - max(m.fg3a_rate) FILTER (WHERE m.window_name = 'season') OVER anchor
            AS fg3a_rate_delta_season,
        m.rim_freq,
        m.rim_freq - CASE
            WHEN m.window_name = 'season'
                THEN max(m.rim_freq) FILTER (WHERE m.window_name = 'prior_season') OVER anchor
            ELSE lag(m.rim_freq, m.n) OVER back
        END AS rim_freq_delta_prior,
        m.rim_freq - max(m.rim_freq) FILTER (WHERE m.window_name = 'season') OVER anchor
            AS rim_freq_delta_season,
        m.short_mid_freq,
        m.short_mid_freq - CASE
            WHEN m.window_name = 'season'
                THEN max(m.short_mid_freq) FILTER (WHERE m.window_name = 'prior_season') OVER anchor
            ELSE lag(m.short_mid_freq, m.n) OVER back
        END AS short_mid_freq_delta_prior,
        m.short_mid_freq - max(m.short_mid_freq) FILTER (WHERE m.window_name = 'season') OVER anchor
            AS short_mid_freq_delta_season,
        m.long_mid_freq,
        m.long_mid_freq - CASE
            WHEN m.window_name = 'season'
                THEN max(m.long_mid_freq) FILTER (WHERE m.window_name = 'prior_season') OVER anchor
            ELSE lag(m.long_mid_freq, m.n) OVER back
        END AS long_mid_freq_delta_prior,
        m.long_mid_freq - max(m.long_mid_freq) FILTER (WHERE m.window_name = 'season') OVER anchor
            AS long_mid_freq_delta_season,
        m.corner_3_freq,
        m.corner_3_freq - CASE
            WHEN m.window_name = 'season'
                THEN max(m.corner_3_freq) FILTER (WHERE m.window_name = 'prior_season') OVER anchor
            ELSE lag(m.corner_3_freq, m.n) OVER back
        END AS corner_3_freq_delta_prior,
        m.corner_3_freq - max(m.corner_3_freq) FILTER (WHERE m.window_name = 'season') OVER anchor
            AS corner_3_freq_delta_season,
        m.above_break_3_freq,
        m.above_break_3_freq - CASE
            WHEN m.window_name = 'season'
                THEN max(m.above_break_3_freq)
                    FILTER (WHERE m.window_name = 'prior_season') OVER anchor
            ELSE lag(m.above_break_3_freq, m.n) OVER back
        END AS above_break_3_freq_delta_prior,
        m.above_break_3_freq - max(m.above_break_3_freq)
            FILTER (WHERE m.window_name = 'season') OVER anchor
            AS above_break_3_freq_delta_season,
        m.rim_fg_pct,
        m.rim_fg_pct - CASE
            WHEN m.window_name = 'season'
                THEN max(m.rim_fg_pct) FILTER (WHERE m.window_name = 'prior_season') OVER anchor
            ELSE lag(m.rim_fg_pct, m.n) OVER back
        END AS rim_fg_pct_delta_prior,
        m.rim_fg_pct - max(m.rim_fg_pct) FILTER (WHERE m.window_name = 'season') OVER anchor
            AS rim_fg_pct_delta_season,
        m.short_mid_fg_pct,
        m.short_mid_fg_pct - CASE
            WHEN m.window_name = 'season'
                THEN max(m.short_mid_fg_pct)
                    FILTER (WHERE m.window_name = 'prior_season') OVER anchor
            ELSE lag(m.short_mid_fg_pct, m.n) OVER back
        END AS short_mid_fg_pct_delta_prior,
        m.short_mid_fg_pct - max(m.short_mid_fg_pct)
            FILTER (WHERE m.window_name = 'season') OVER anchor
            AS short_mid_fg_pct_delta_season,
        m.long_mid_fg_pct,
        m.long_mid_fg_pct - CASE
            WHEN m.window_name = 'season'
                THEN max(m.long_mid_fg_pct)
                    FILTER (WHERE m.window_name = 'prior_season') OVER anchor
            ELSE lag(m.long_mid_fg_pct, m.n) OVER back
        END AS long_mid_fg_pct_delta_prior,
        m.long_mid_fg_pct - max(m.long_mid_fg_pct)
            FILTER (WHERE m.window_name = 'season') OVER anchor
            AS long_mid_fg_pct_delta_season,
        m.corner_3_fg_pct,
        m.corner_3_fg_pct - CASE
            WHEN m.window_name = 'season'
                THEN max(m.corner_3_fg_pct)
                    FILTER (WHERE m.window_name = 'prior_season') OVER anchor
            ELSE lag(m.corner_3_fg_pct, m.n) OVER back
        END AS corner_3_fg_pct_delta_prior,
        m.corner_3_fg_pct - max(m.corner_3_fg_pct)
            FILTER (WHERE m.window_name = 'season') OVER anchor
            AS corner_3_fg_pct_delta_season,
        m.above_break_3_fg_pct,
        m.above_break_3_fg_pct - CASE
            WHEN m.window_name = 'season'
                THEN max(m.above_break_3_fg_pct)
                    FILTER (WHERE m.window_name = 'prior_season') OVER anchor
            ELSE lag(m.above_break_3_fg_pct, m.n) OVER back
        END AS above_break_3_fg_pct_delta_prior,
        m.above_break_3_fg_pct - max(m.above_break_3_fg_pct)
            FILTER (WHERE m.window_name = 'season') OVER anchor
            AS above_break_3_fg_pct_delta_season,
        m.pts_per36,
        m.pts_per36 - CASE
            WHEN m.window_name = 'season'
                THEN max(m.pts_per36) FILTER (WHERE m.window_name = 'prior_season') OVER anchor
            ELSE lag(m.pts_per36, m.n) OVER back
        END AS pts_per36_delta_prior,
        m.pts_per36 - max(m.pts_per36) FILTER (WHERE m.window_name = 'season') OVER anchor
            AS pts_per36_delta_season,
        m.ast_per36,
        m.ast_per36 - CASE
            WHEN m.window_name = 'season'
                THEN max(m.ast_per36) FILTER (WHERE m.window_name = 'prior_season') OVER anchor
            ELSE lag(m.ast_per36, m.n) OVER back
        END AS ast_per36_delta_prior,
        m.ast_per36 - max(m.ast_per36) FILTER (WHERE m.window_name = 'season') OVER anchor
            AS ast_per36_delta_season,
        m.reb_per36,
        m.reb_per36 - CASE
            WHEN m.window_name = 'season'
                THEN max(m.reb_per36) FILTER (WHERE m.window_name = 'prior_season') OVER anchor
            ELSE lag(m.reb_per36, m.n) OVER back
        END AS reb_per36_delta_prior,
        m.reb_per36 - max(m.reb_per36) FILTER (WHERE m.window_name = 'season') OVER anchor
            AS reb_per36_delta_season,
        m.pts_per100,
        m.pts_per100 - CASE
            WHEN m.window_name = 'season'
                THEN max(m.pts_per100) FILTER (WHERE m.window_name = 'prior_season') OVER anchor
            ELSE lag(m.pts_per100, m.n) OVER back
        END AS pts_per100_delta_prior,
        m.pts_per100 - max(m.pts_per100) FILTER (WHERE m.window_name = 'season') OVER anchor
            AS pts_per100_delta_season,
        m.ast_per100,
        m.ast_per100 - CASE
            WHEN m.window_name = 'season'
                THEN max(m.ast_per100) FILTER (WHERE m.window_name = 'prior_season') OVER anchor
            ELSE lag(m.ast_per100, m.n) OVER back
        END AS ast_per100_delta_prior,
        m.ast_per100 - max(m.ast_per100) FILTER (WHERE m.window_name = 'season') OVER anchor
            AS ast_per100_delta_season,
        m.reb_per100,
        m.reb_per100 - CASE
            WHEN m.window_name = 'season'
                THEN max(m.reb_per100) FILTER (WHERE m.window_name = 'prior_season') OVER anchor
            ELSE lag(m.reb_per100, m.n) OVER back
        END AS reb_per100_delta_prior,
        m.reb_per100 - max(m.reb_per100) FILTER (WHERE m.window_name = 'season') OVER anchor
            AS reb_per100_delta_season,
        m.game_score,
        m.game_score - CASE
            WHEN m.window_name = 'season'
                THEN max(m.game_score) FILTER (WHERE m.window_name = 'prior_season') OVER anchor
            ELSE lag(m.game_score, m.n) OVER back
        END AS game_score_delta_prior,
        m.game_score - max(m.game_score) FILTER (WHERE m.window_name = 'season') OVER anchor
            AS game_score_delta_season,
        i.epm,
        i.epm_o,
        i.epm_d,
        i.darko,
        i.darko_o,
        i.darko_d,
        i.bpm_local,
        i.bpm_local_o,
        i.bpm_local_d,
        CASE WHEN m.window_name = 'prior_season' THEN f.ws_local ELSE v.ws_local END AS ws_local,
        CASE WHEN m.window_name = 'prior_season' THEN f.ws_local_o ELSE v.ws_local_o END
            AS ws_local_o,
        CASE WHEN m.window_name = 'prior_season' THEN f.ws_local_d ELSE v.ws_local_d END
            AS ws_local_d,
        CASE WHEN m.window_name = 'prior_season' THEN f.vorp_local ELSE v.vorp_local END
            AS vorp_local
    FROM prod_all m
    LEFT JOIN impact_asof i ON i.player_id = m.player_id AND i.game_date = m.game_date
    LEFT JOIN value_asof v ON v.player_id = m.player_id AND v.game_date = m.game_date
    LEFT JOIN value_final f
        ON f.player_id = m.player_id
        AND f.start_year = left(m.season, 4)::int - 1
        AND m.window_name = 'prior_season'
    WINDOW
        back AS (PARTITION BY m.player_id, m.season, m.window_name ORDER BY m.k),
        anchor AS (PARTITION BY m.player_id, m.season, m.k)
)

-- Role rows and production rows side by side, each NULL in the other's columns. A production row's
-- team_id is NULL, which equals nothing, so no pair matches and each row comes out once; joining
-- on it rather than on false lets the planner hash or merge rather than compare every pair.
SELECT
    coalesce(r.player_id, p.player_id) AS player_id,
    r.team_id,
    coalesce(r.game_date, p.game_date) AS game_date,
    coalesce(r.window_name, p.window_name) AS "window",
    coalesce(r.games_in_window, p.games_in_window) AS games_in_window,
    coalesce(r.is_short, p.is_short) AS is_short,
    r.games_played,
    r.games_started,
    r.minutes_per_game,
    r.minutes_per_game_delta_prior,
    r.minutes_per_game_delta_season,
    r.minutes_share,
    r.minutes_share_delta_prior,
    r.minutes_share_delta_season,
    r.minutes_volatility,
    r.minutes_volatility_delta_prior,
    r.minutes_volatility_delta_season,
    r.starter_rate,
    r.starter_rate_delta_prior,
    r.starter_rate_delta_season,
    r.closer_rate,
    r.closer_rate_delta_prior,
    r.closer_rate_delta_season,
    r.rotation_rank,
    r.rotation_rank_delta_prior,
    r.rotation_rank_delta_season,
    r.dnp_cd_count,
    r.dnp_cd_count_delta_prior,
    r.dnp_cd_count_delta_season,
    r.inactive_count,
    r.inactive_count_delta_prior,
    r.inactive_count_delta_season,
    r.on_court_ortg,
    r.on_court_ortg_delta_prior,
    r.on_court_ortg_delta_season,
    r.on_court_drtg,
    r.on_court_drtg_delta_prior,
    r.on_court_drtg_delta_season,
    r.on_court_net,
    r.on_court_net_delta_prior,
    r.on_court_net_delta_season,
    r.on_off_net,
    r.on_off_net_delta_prior,
    r.on_off_net_delta_season,
    r.on_court_poss,
    r.on_court_opp_poss,
    r.off_court_poss,
    r.off_court_opp_poss,
    p.ts_pct,
    p.ts_pct_delta_prior,
    p.ts_pct_delta_season,
    p.efg_pct,
    p.efg_pct_delta_prior,
    p.efg_pct_delta_season,
    p.usg_pct,
    p.usg_pct_delta_prior,
    p.usg_pct_delta_season,
    p.ast_pct,
    p.ast_pct_delta_prior,
    p.ast_pct_delta_season,
    p.tov_pct,
    p.tov_pct_delta_prior,
    p.tov_pct_delta_season,
    p.oreb_pct,
    p.oreb_pct_delta_prior,
    p.oreb_pct_delta_season,
    p.dreb_pct,
    p.dreb_pct_delta_prior,
    p.dreb_pct_delta_season,
    p.ftr,
    p.ftr_delta_prior,
    p.ftr_delta_season,
    p.fg3a_rate,
    p.fg3a_rate_delta_prior,
    p.fg3a_rate_delta_season,
    p.rim_freq,
    p.rim_freq_delta_prior,
    p.rim_freq_delta_season,
    p.short_mid_freq,
    p.short_mid_freq_delta_prior,
    p.short_mid_freq_delta_season,
    p.long_mid_freq,
    p.long_mid_freq_delta_prior,
    p.long_mid_freq_delta_season,
    p.corner_3_freq,
    p.corner_3_freq_delta_prior,
    p.corner_3_freq_delta_season,
    p.above_break_3_freq,
    p.above_break_3_freq_delta_prior,
    p.above_break_3_freq_delta_season,
    p.rim_fg_pct,
    p.rim_fg_pct_delta_prior,
    p.rim_fg_pct_delta_season,
    p.short_mid_fg_pct,
    p.short_mid_fg_pct_delta_prior,
    p.short_mid_fg_pct_delta_season,
    p.long_mid_fg_pct,
    p.long_mid_fg_pct_delta_prior,
    p.long_mid_fg_pct_delta_season,
    p.corner_3_fg_pct,
    p.corner_3_fg_pct_delta_prior,
    p.corner_3_fg_pct_delta_season,
    p.above_break_3_fg_pct,
    p.above_break_3_fg_pct_delta_prior,
    p.above_break_3_fg_pct_delta_season,
    p.pts_per36,
    p.pts_per36_delta_prior,
    p.pts_per36_delta_season,
    p.ast_per36,
    p.ast_per36_delta_prior,
    p.ast_per36_delta_season,
    p.reb_per36,
    p.reb_per36_delta_prior,
    p.reb_per36_delta_season,
    p.pts_per100,
    p.pts_per100_delta_prior,
    p.pts_per100_delta_season,
    p.ast_per100,
    p.ast_per100_delta_prior,
    p.ast_per100_delta_season,
    p.reb_per100,
    p.reb_per100_delta_prior,
    p.reb_per100_delta_season,
    p.game_score,
    p.game_score_delta_prior,
    p.game_score_delta_season,
    p.epm,
    p.epm_o,
    p.epm_d,
    p.darko,
    p.darko_o,
    p.darko_d,
    p.bpm_local,
    p.bpm_local_o,
    p.bpm_local_d,
    p.ws_local,
    p.ws_local_o,
    p.ws_local_d,
    p.vorp_local
FROM role_row r
FULL JOIN prod_row p ON p.team_id = r.team_id;

-- The unique key REFRESH ... CONCURRENTLY diffs by; a production row's team_id is NULL, so NULLs
-- compare equal here.
CREATE UNIQUE INDEX uq_player_window_stats_player_id_team_id_game_date_window
    ON player_window_stats (player_id, team_id, game_date, "window") NULLS NOT DISTINCT;
