from __future__ import annotations

import os
import time
import unittest
from datetime import date, datetime, timezone
from types import SimpleNamespace

from main import (
    EASTERN,
    OutlookCandidate,
    as_date,
    best_fit_points,
    blended_player_average,
    free_agent_rejection_reason,
    hydrate_free_agents,
    is_protected_roster_player,
    load_lineup_configuration,
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

    def test_projection_is_blended_with_actual_and_recent_averages(self):
        self.assertAlmostEqual(blended_player_average(22.0, 18.0, 30.0, 28.0, 25.0), 22.25)
        self.assertEqual(blended_player_average(0.0, 18.0, None, None, None), 18.0)
        self.assertAlmostEqual(blended_player_average(0.0, 0.0, 7.0, 8.0, 9.0), 8.0)

    def test_pickup_ownership_and_scoring_guardrails(self):
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
        self.assertEqual(
            free_agent_rejection_reason(make_candidate(percent_owned=50.1)),
            "ownership above 50%",
        )
        self.assertEqual(
            free_agent_rejection_reason(make_candidate(projected_avg=31.0)),
            "FPPG above 30",
        )
        self.assertEqual(
            free_agent_rejection_reason(make_candidate(percent_owned=None)),
            "ownership unavailable",
        )
        self.assertTrue(is_protected_roster_player(make_candidate(percent_owned=92.0)))
        self.assertTrue(is_protected_roster_player(make_candidate(season_avg=31.0)))
        self.assertTrue(is_protected_roster_player(make_candidate(percent_owned=None)))
        self.assertFalse(is_protected_roster_player(make_candidate(percent_owned=12.0)))


if __name__ == "__main__":
    unittest.main()
