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
    hydrate_free_agents,
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

    def test_free_agents_are_rehydrated_in_batches_with_schedule_data(self):
        original = [
            SimpleNamespace(playerId=1),
            SimpleNamespace(playerId=2),
            SimpleNamespace(playerId=3),
        ]
        fetched_batches = []

        class FakeLeague:
            def player_info(self, playerId):
                fetched_batches.append(list(playerId))
                players = [FakePlayer(pid, "PG", ["PG", "G", "UT"], {"game": {"date": datetime(2026, 11, 17)}})
                           for pid in playerId]
                return players

        hydrated = hydrate_free_agents(FakeLeague(), original, batch_size=2)
        self.assertEqual([p.playerId for p in hydrated], [1, 2, 3])
        self.assertEqual([len(batch) for batch in fetched_batches], [2, 1])
        self.assertTrue(all(player.schedule for player in hydrated))

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

    def test_projection_preferred_but_missing_inputs_do_not_disqualify_player(self):
        self.assertEqual(blended_player_average(22.0, 18.0, 30.0, 28.0, 25.0), 22.0)
        self.assertEqual(blended_player_average(0.0, 18.0, None, None, None), 18.0)
        self.assertEqual(blended_player_average(0.0, 0.0, 7.0, 8.0, 9.0), 9.0)


if __name__ == "__main__":
    unittest.main()
