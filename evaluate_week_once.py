"""One-time ESPN-applied scoring audit for the Nov 16-22, 2026 waiver forecast."""
from __future__ import annotations

import json
import os
import re
from datetime import date, datetime

from espn_api.basketball import League

from main import EASTERN, as_date, player_game_log_records

LEAGUE_ID = 1850516159
TEAM_ID = 12
SEASON_ID = 2027
WEEK_START = date(2026, 11, 16)
WEEK_END = date(2026, 11, 22)

RECOMMENDATIONS = [
    {"pickup": "Robert Williams III", "drop": "Reed Sheppard"},
    {"pickup": "Draymond Green", "drop": "Naji Marshall"},
    {"pickup": "Cameron Johnson", "drop": "Kyle Kuzma"},
]
WATCHLIST = ["Adem Bona", "Tacko Fall"]


def normalized_name(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def scheduled_dates(player: object) -> list[date]:
    dates = set()
    for game in (getattr(player, "schedule", {}) or {}).values():
        game_date = as_date(
            game.get("date") if isinstance(game, dict)
            else getattr(game, "date", None)
        )
        if game_date is not None and WEEK_START <= game_date <= WEEK_END:
            dates.add(game_date)
    return sorted(dates)


def main() -> int:
    today = datetime.now(EASTERN).date()
    if today < WEEK_END:
        raise RuntimeError("The target scoring week has not completed.")
    espn_s2 = os.environ.get("ESPN_S2")
    swid = os.environ.get("ESPN_SWID")
    if not espn_s2 or not swid:
        raise RuntimeError("Missing ESPN authentication secrets.")

    league = League(
        league_id=LEAGUE_ID,
        year=SEASON_ID,
        espn_s2=espn_s2,
        swid=swid,
    )
    team = next((item for item in league.teams if int(item.team_id) == TEAM_ID), None)
    if team is None:
        raise RuntimeError(f"Fantasy team {TEAM_ID} not found.")

    pool = list(getattr(team, "roster", []) or [])
    pool.extend(league.free_agents(size=500))
    ids_by_name = {}
    for item in pool:
        name = normalized_name(getattr(item, "name", ""))
        player_id = getattr(item, "playerId", None)
        if name and player_id is not None:
            ids_by_name[name] = player_id

    names = (
        {row["pickup"] for row in RECOMMENDATIONS}
        | {row["drop"] for row in RECOMMENDATIONS}
        | set(WATCHLIST)
    )
    results = {}
    for name in sorted(names):
        player_id = ids_by_name.get(normalized_name(name))
        if player_id is None:
            player_id = league.player_map.get(name)
        if player_id is None:
            results[name] = {"status": "unavailable", "reason": "ESPN player ID not resolved"}
            continue

        player = league.player_info(playerId=player_id)
        if isinstance(player, list):
            player = player[0] if player else None
        if player is None:
            results[name] = {"status": "unavailable", "reason": "ESPN player card unavailable"}
            continue

        games = scheduled_dates(player)
        records = [
            row for row in player_game_log_records(player, WEEK_END)
            if row["date"] is not None and WEEK_START <= row["date"] <= WEEK_END
        ]
        played_records = [
            row for row in records
            if row.get("minutes") is not None and float(row["minutes"]) > 0
        ]
        results[name] = {
            "status": "ok" if records else "no_game_stats",
            "player_id": player_id,
            "nba_team": getattr(player, "proTeam", None),
            "scheduled_dates": [item.isoformat() for item in games],
            "scheduled_games": len(games),
            "played_games": len(played_records),
            "dnp_or_minutes_missing_dates": [
                row["date"].isoformat() for row in records
                if row.get("minutes") is None
            ],
            "actual_espn_fantasy_points": round(sum(float(row["points"]) for row in records), 2),
            "game_points": [
                {
                    "date": row["date"].isoformat(),
                    "fantasy_points": round(float(row["points"]), 2),
                    "minutes": row.get("minutes"),
                }
                for row in records
            ],
            "scoring_source": "ESPN player-card per-game applied_total (league-applied fantasy scoring)",
        }

    settings = getattr(league, "settings", None)
    output = {
        "week_start": WEEK_START.isoformat(),
        "week_end": WEEK_END.isoformat(),
        "league_id": LEAGUE_ID,
        "team_id": TEAM_ID,
        "scoring_type": getattr(settings, "scoring_type", None),
        "recommendations": RECOMMENDATIONS,
        "missed_opportunity_watchlist": WATCHLIST,
        "players": results,
    }
    print("WAIVER_ACTUALS_JSON=" + json.dumps(output, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
