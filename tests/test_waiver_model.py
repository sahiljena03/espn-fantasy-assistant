from __future__ import annotations

import os
import time
import unittest
from unittest.mock import patch
from datetime import date, datetime, timezone
from types import SimpleNamespace

from main import (
    EASTERN,
    OutlookCandidate,
    as_date,
    best_fit_assignments,
    best_fit_points,
    blended_player_average,
    free_agent_rejection_reason,
    hydrate_free_agents,
    is_protected_roster_player,
    load_lineup_configuration,
    parse_espn_injury_signals,
    parse_espn_news_signals,
    pickup_lineup_game_usage,
    player_can_fill_slot,
    minutes_opportunity_watch,
    render_team_schedule_targets,
    team_game_counts,
    _best_swap,
)


class FakePlayer:
    def __init__(self, player_id: int, position: str, eligible_slots: list[str], schedule=None):
        self.playerId = player_id
        self.position = position
        self.eligibleSlots = eligible_slots
        self.schedule = schedule or {}


class WaiverModelTests(unittest.TestCase):
    def test_utc_game_timestamp_is_converted_to_eastern_calendar_day(self):
        timestamp = datetime(2026, 11, 17, 0, 30, tzinfo=timezone.utc)
        self.assertEqual(as_date(timestamp), date(2026, 11, 16))

    def test_naive_runner_datetime_uses_runner_timezone_before_et_conversion(self):
        previous_tz = os.environ.get("TZ")
        try:
            os.environ["TZ"] = "UTC"
            if hasattr(time, "tzset"):
                time.tzset()
            self.assertEqual(as_date(datetime(2026, 11, 17, 0, 30)), date(2026, 11, 16))
        finally:
            if previous_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = previous_tz
            if hasattr(time, "tzset"):
                time.tzset()

    def test_free_agents_are_rehydrated_with_schedule_and_recent_splits(self):
        original = [
            SimpleNamespace(playerId=1),
            SimpleNamespace(playerId=2),
            SimpleNamespace(playerId=3),
        ]
        fetched_batches = []
        season = 2027
        game_timestamp = datetime(2026, 11, 17, 0, 30, tzinfo=timezone.utc).timestamp() * 1000

        class FakeRequests:
            def get_player_card(self, playerIds, max_scoring_period, additional_filters):
                fetched_batches.append((list(playerIds), max_scoring_period, list(additional_filters)))
                wraps = []
                for player_id in playerIds:
                    stats = []
                    values = {"00": 18.0, "10": 22.0, "01": 25.0, "02": 21.0, "03": 19.0}
                    for prefix, average in values.items():
                        stats.append({
                            "id": f"{prefix}{season}",
                            "seasonId": season,
                            "scoringPeriodId": 0,
                            "appliedTotal": average * 10,
                            "appliedAverage": average,
                        })
                    wraps.append({
                        "id": player_id,
                        "fullName": f"Player {player_id}",
                        "defaultPositionId": 1,
                        "lineupSlotId": 0,
                        "eligibleSlots": [0, 5, 11],
                        "acquisitionType": "FREEAGENT",
                        "proTeamId": 2,
                        "injuryStatus": "ACTIVE",
                        "playerPoolEntry": {
                            "player": {
                                "stats": stats,
                                "injured": False,
                                "ownership": {"percentOwned": 42.5, "percentStarted": 11.0},
                            }
                        },
                    })
                return {"players": wraps}

        class FakeLeague:
            def __init__(self):
                self.year = season
                self.finalScoringPeriod = 21
                self.pro_schedule = {
                    2: {
                        "game_1": [{
                            "awayProTeamId": 2,
                            "homeProTeamId": 1,
                            "date": game_timestamp,
                        }]
                    }
                }
                self.espn_request = FakeRequests()

        hydrated = hydrate_free_agents(FakeLeague(), original, batch_size=2)
        self.assertEqual([player.playerId for player in hydrated], [1, 2, 3])
        self.assertEqual([len(batch[0]) for batch in fetched_batches], [2, 1])
        self.assertTrue(all(player.schedule for player in hydrated))
        self.assertTrue(all(player.avg_points == 18.0 for player in hydrated))
        self.assertTrue(all(player.projected_avg_points == 22.0 for player in hydrated))
        self.assertTrue(all("2027_last_7" in player.stats for player in hydrated))
        self.assertTrue(all("2027_last_15" in player.stats for player in hydrated))
        self.assertTrue(all("2027_last_30" in player.stats for player in hydrated))
        self.assertTrue(all(player.percent_owned == 42.5 for player in hydrated))
        self.assertTrue(all(player.percent_started == 11.0 for player in hydrated))
        self.assertTrue(all(
            batch[2] == ["012027", "022027", "032027"]
            for batch in fetched_batches
        ))


    def test_utility_slot_requires_explicit_espn_eligibility(self):
        guard_only = FakePlayer(1, "PG", ["PG", "G"])
        utility_eligible = FakePlayer(2, "PG", ["PG", "G", "UT"])
        self.assertFalse(player_can_fill_slot(guard_only, "UT"))
        self.assertTrue(player_can_fill_slot(utility_eligible, "UT"))

        forward = FakePlayer(3, "PF", ["PF", "F", "UT"])
        guard = FakePlayer(4, "SG", ["SG", "G", "UT"])
        self.assertTrue(player_can_fill_slot(forward, "F/C"))
        self.assertTrue(player_can_fill_slot(forward, "G/F"))
        self.assertFalse(player_can_fill_slot(guard, "F/C"))

    def test_pickup_must_fit_every_expected_available_game_after_drop(self):
        first_day = date(2026, 11, 16)
        second_day = date(2026, 11, 18)
        game_dates = [first_day, second_day]
        game_probabilities = {first_day: 1.0, second_day: 1.0}

        pg = FakePlayer(1, "PG", ["PG", "G", "UT"])
        wing = FakePlayer(2, "SF", ["SF", "F", "UT"])
        pickup = FakePlayer(3, "PF", ["PF", "F", "UT"])

        def make_candidate(name, position, average):
            return OutlookCandidate(
                name=name,
                position=position,
                nba_team="BOS",
                injury="ACTIVE",
                expected_return=None,
                game_dates=game_dates,
                game_probabilities=game_probabilities,
                season_avg=average,
                projected_avg=average,
                last_7=average,
                last_15=average,
                last_30=average,
                blended_avg=average,
                injury_multiplier=1.0,
                expected_games=2.0,
                estimated_points=average * 2.0,
                percent_owned=10.0,
                percent_started=5.0,
                latest_news="",
                pos_rank=50,
                data_sources=5,
                confidence="High",
                lineup_slot="",
            )

        candidates = {
            1: make_candidate("Starting point guard", "PG", 30.0),
            2: make_candidate("Starting wing", "SF", 28.0),
            3: make_candidate("Waiver forward", "PF", 25.0),
        }

        active_games_with_full_roster = pickup_lineup_game_usage(
            3, [pg, wing, pickup], candidates, ["PG", "UT"], True
        )
        self.assertEqual(active_games_with_full_roster, (0, 2))

        active_games_after_dropping_wing = pickup_lineup_game_usage(
            3, [pg, pickup], candidates, ["PG", "UT"], True
        )
        self.assertEqual(active_games_after_dropping_wing, (2, 2))

        assignments = best_fit_assignments(
            [pg, pickup],
            {1: 60.0, 3: 50.0},
            ["PG", "UT"],
        )
        self.assertEqual(assignments, {1: "PG", 3: "UT"})

        low_value_roster_player = FakePlayer(4, "SF", ["SF", "F", "UT"])
        candidates[4] = make_candidate("Drop candidate", "SF", 15.0)
        blocked = _best_swap(
            [pg, wing, low_value_roster_player],
            [pickup],
            candidates,
            0.0,
            ["PG", "UT"],
            True,
            set(),
            {4},
            set(),
        )
        self.assertIsNone(blocked)

        fully_playable = _best_swap(
            [pg, low_value_roster_player],
            [pickup],
            candidates,
            0.0,
            ["PG", "UT"],
            True,
            set(),
            {4},
            set(),
        )
        self.assertIsNotNone(fully_playable)
        self.assertEqual(fully_playable[4:], (2, 2))

    def test_slot_optimizer_uses_position_eligibility(self):
        pg_weak = FakePlayer(1, "PG", ["PG", "G", "UT"])
        pg_strong = FakePlayer(2, "PG", ["PG", "G", "UT"])
        wing = FakePlayer(3, "SF", ["SF", "F", "UT"])
        points = best_fit_points(
            [pg_weak, pg_strong, wing],
            {1: 20.0, 2: 25.0, 3: 30.0},
            ["PG", "UT"],
        )
        self.assertEqual(points, 55.0)

    def test_lineup_settings_exclude_bench_and_ir_and_detect_daily_lock(self):
        class FakeRequests:
            def league_get(self, params):
                self.params = params
                return {
                    "settings": {
                        "rosterSettings": {
                            "lineupSlotCounts": {"0": 1, "11": 2, "12": 4, "13": 1},
                            "lineupLocktimeType": "INDIVIDUAL_GAME",
                        }
                    }
                }

        league = SimpleNamespace(espn_request=FakeRequests())
        slots, daily, mode = load_lineup_configuration(league)
        self.assertEqual(slots, ["PG", "UT", "UT"])
        self.assertTrue(daily)
        self.assertIn("daily", mode)


    def test_only_original_roster_players_can_be_swap_drop_candidates(self):
        original_roster_player = FakePlayer(1, "PG", ["PG", "UT"])
        earlier_hypothetical_pickup = FakePlayer(2, "PG", ["PG", "UT"])
        new_pickup = FakePlayer(3, "PG", ["PG", "UT"])

        def make_candidate(name, points):
            return OutlookCandidate(
                name=name,
                position="PG",
                nba_team="BOS",
                injury="ACTIVE",
                expected_return=None,
                game_dates=[date(2026, 11, 16)],
                game_probabilities={date(2026, 11, 16): 1.0},
                season_avg=points,
                projected_avg=points,
                last_7=None,
                last_15=None,
                last_30=None,
                blended_avg=points,
                injury_multiplier=1.0,
                expected_games=1.0,
                estimated_points=points,
                percent_owned=10.0,
                percent_started=5.0,
                latest_news="",
                pos_rank=50,
                data_sources=2,
                confidence="Medium",
                lineup_slot="",
            )

        candidates = {
            1: make_candidate("Actual roster player", 20.0),
            2: make_candidate("Earlier hypothetical pickup", 5.0),
            3: make_candidate("New pickup", 25.0),
        }
        best = _best_swap(
            [original_roster_player, earlier_hypothetical_pickup],
            [new_pickup],
            candidates,
            25.0,
            [],
            True,
            set(),
            {1},
        )
        self.assertIsNotNone(best)
        self.assertEqual(best[2].playerId, 1)

    def test_espn_injury_parser_extracts_out_players_and_team(self):
        payload = {
            "injuries": [{
                "team": {"id": "2", "abbreviation": "BOS"},
                "injuries": [{
                    "athlete": {"id": "3975", "displayName": "Stephen Curry"},
                    "status": "Out",
                    "type": {"name": "Ankle"},
                    "date": "2026-11-15T00:00Z",
                }],
            }],
        }
        signals = parse_espn_injury_signals(payload)
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0]["player_id"], "3975")
        self.assertEqual(signals[0]["team_abbr"], "BOS")
        self.assertEqual(signals[0]["status"], "OUT")


    def test_team_game_counts_deduplicates_players_by_team(self):
        gsw_schedule = {
            "game_1": {"date": datetime(2026, 11, 16, 17)},
            "game_2": {"date": datetime(2026, 11, 18, 17)},
            "game_3": {"date": datetime(2026, 11, 20, 17)},
            "game_4": {"date": datetime(2026, 11, 22, 17)},
        }
        nyk_schedule = {
            "game_5": {"date": datetime(2026, 11, 17, 17)},
            "game_6": {"date": datetime(2026, 11, 21, 17)},
        }
        players = [
            SimpleNamespace(proTeam="GSW", schedule=gsw_schedule),
            SimpleNamespace(proTeam="GSW", schedule=dict(gsw_schedule)),
            SimpleNamespace(proTeam="NYK", schedule=nyk_schedule),
            SimpleNamespace(proTeam="FA", schedule=gsw_schedule),
        ]
        counts = team_game_counts(
            players,
            date(2026, 11, 16),
            date(2026, 11, 22),
        )
        self.assertEqual(counts, {"GSW": 4, "NYK": 2})

    def test_team_schedule_report_separates_four_game_targets_and_two_game_avoids(self):
        players = [
            SimpleNamespace(
                proTeam="GSW",
                schedule={
                    "a": {"date": datetime(2026, 11, 16, 17)},
                    "b": {"date": datetime(2026, 11, 18, 17)},
                    "c": {"date": datetime(2026, 11, 20, 17)},
                    "d": {"date": datetime(2026, 11, 22, 17)},
                },
            ),
            SimpleNamespace(
                proTeam="NYK",
                schedule={
                    "e": {"date": datetime(2026, 11, 17, 17)},
                    "f": {"date": datetime(2026, 11, 21, 17)},
                },
            ),
            SimpleNamespace(
                proTeam="BOS",
                schedule={
                    "g": {"date": datetime(2026, 11, 16, 17)},
                    "h": {"date": datetime(2026, 11, 18, 17)},
                    "i": {"date": datetime(2026, 11, 20, 17)},
                },
            ),
        ]
        report = render_team_schedule_targets(
            players,
            date(2026, 11, 16),
            date(2026, 11, 22),
        )
        self.assertIn("**Target (4 games):** GSW (4)", report)
        self.assertIn("**Avoid (2 games):** NYK (2)", report)
        self.assertNotIn("BOS", report)

    def test_minutes_watch_matches_out_teammate_to_same_position_free_agent(self):
        injured_player = SimpleNamespace(
            playerId=3975,
            name="Stephen Curry",
            proTeam="GSW",
            position="PG",
        )
        bench_candidate = SimpleNamespace(
            playerId=9988,
            name="Bench Guard",
            proTeam="GSW",
            position="PG",
        )
        candidate = OutlookCandidate(
            name="Bench Guard",
            position="PG",
            nba_team="GSW",
            injury="ACTIVE",
            expected_return=None,
            game_dates=[date(2026, 11, 16)],
            game_probabilities={date(2026, 11, 16): 1.0},
            season_avg=12.0,
            projected_avg=13.0,
            last_7=15.0,
            last_15=14.0,
            last_30=13.0,
            blended_avg=13.0,
            injury_multiplier=1.0,
            expected_games=1.0,
            estimated_points=13.0,
            percent_owned=4.0,
            percent_started=3.0,
            latest_news="",
            pos_rank=100,
            data_sources=5,
            confidence="High",
            lineup_slot="",
        )
        injury_payload = {
            "injuries": [{
                "team": {"id": "9", "abbreviation": "GSW"},
                "injuries": [{
                    "athlete": {"id": "3975", "displayName": "Stephen Curry"},
                    "status": "Out",
                    "type": {"name": "Ankle"},
                    "date": "2026-11-15T00:00Z",
                }],
            }],
        }
        with patch(
            "main.fetch_espn_json",
            side_effect=[injury_payload, {"articles": []}],
        ):
            report = minutes_opportunity_watch(
                [bench_candidate],
                [bench_candidate],
                {9988: candidate},
                [injured_player],
                date(2026, 11, 15),
            )
        self.assertIn("Bench Guard", report)
        self.assertIn("Stephen Curry (OUT, Ankle)", report)
        self.assertIn("Fantasy started %", report)
        self.assertIn("No extra minutes or fantasy points", report)

    def test_espn_news_parser_flags_recent_rest_and_load_management(self):
        player = SimpleNamespace(
            playerId=3975,
            name="Stephen Curry",
            proTeam="GSW",
            position="PG",
        )
        payload = {
            "articles": [{
                "headline": "Stephen Curry to sit for rest",
                "description": "Golden State lists Stephen Curry for load management.",
                "published": "2026-11-15T12:00:00Z",
                "links": {"web": {"href": "https://www.espn.com/nba/story/example"}},
            }],
        }
        signals = parse_espn_news_signals(
            payload,
            {"stephen curry": [player]},
            date(2026, 11, 15),
        )
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0]["status"], "REST/LOAD-MANAGEMENT NEWS")
        self.assertEqual(signals[0]["team_abbr"], "GSW")
        self.assertIn("espn.com", signals[0]["source_url"])

    def test_projection_is_blended_with_actual_and_recent_averages(self):
        self.assertAlmostEqual(blended_player_average(22.0, 18.0, 30.0, 28.0, 25.0), 22.25)
        self.assertEqual(blended_player_average(0.0, 18.0, None, None, None), 18.0)
        self.assertAlmostEqual(blended_player_average(0.0, 0.0, 7.0, 8.0, 9.0), 8.3333333333)

    def test_pickup_ownership_is_informational_and_drop_limit_is_26_fppg(self):
        base = {
            "name": "Test Player",
            "position": "PG",
            "nba_team": "BOS",
            "injury": "ACTIVE",
            "expected_return": None,
            "game_dates": [date(2026, 11, 16)],
            "game_probabilities": {date(2026, 11, 16): 1.0},
            "season_avg": 20.0,
            "projected_avg": 22.0,
            "last_7": 21.0,
            "last_15": 20.0,
            "last_30": 19.0,
            "blended_avg": 20.5,
            "injury_multiplier": 1.0,
            "expected_games": 1.0,
            "estimated_points": 20.5,
            "percent_owned": 50.0,
            "percent_started": 10.0,
            "latest_news": "",
            "pos_rank": 50,
            "data_sources": 5,
            "confidence": "High",
            "lineup_slot": "",
        }

        def make_candidate(**changes):
            values = dict(base)
            values.update(changes)
            return OutlookCandidate(**values)

        self.assertIsNone(free_agent_rejection_reason(make_candidate()))
        self.assertIsNone(free_agent_rejection_reason(make_candidate(percent_owned=99.9)))
        self.assertIsNone(free_agent_rejection_reason(make_candidate(percent_owned=None)))
        self.assertEqual(
            free_agent_rejection_reason(make_candidate(projected_avg=31.0)),
            "FPPG above 30",
        )
        self.assertFalse(is_protected_roster_player(make_candidate(percent_owned=99.9)))
        self.assertFalse(is_protected_roster_player(make_candidate(percent_owned=None)))
        self.assertFalse(is_protected_roster_player(make_candidate(blended_avg=25.99)))
        self.assertTrue(is_protected_roster_player(make_candidate(blended_avg=26.0)))
        self.assertTrue(is_protected_roster_player(make_candidate(blended_avg=29.0)))
        self.assertTrue(is_protected_roster_player(make_candidate(blended_avg=0.0)))


if __name__ == "__main__":
    unittest.main()
