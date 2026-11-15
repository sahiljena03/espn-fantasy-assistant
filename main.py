"""Daily ESPN fantasy basketball waiver report. Read-only; never submits transactions."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from espn_api.basketball import League

EASTERN = ZoneInfo("America/New_York")
LEAGUE_ID = 1850516159
TEAM_ID = 12
TEAM_NAME = "Scottie on the Spectrum"
MAX_WEEKLY_ADDS = 5
FREE_AGENT_POOL_SIZE = 250


@dataclass
class PlayerCandidate:
    name: str
    position: str
    nba_team: str
    injury: str
    avg_points: float
    games_left: int
    projected_week_points: float

@dataclass
class OutlookCandidate:
    name: str
    position: str
    nba_team: str
    injury: str
    expected_return: date | None
    game_dates: list[date]
    season_avg: float
    projected_avg: float
    last_7: float | None
    last_15: float | None
    last_30: float | None
    blended_avg: float
    injury_multiplier: float
    estimated_points: float
    percent_owned: float | None
    percent_started: float | None
    latest_news: str



def season_start_year(today: date) -> int:
    # ESPN basketball season IDs use the year the season ends (e.g. 2026–27 = 2027).
    return today.year + 1 if today.month >= 10 else today.year


def week_bounds(today: date) -> tuple[date, date]:
    monday = today - timedelta(days=today.weekday())
    return monday, monday + timedelta(days=6)


def as_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


def player_games_left(player: object, today: date, sunday: date) -> int:
    schedule = getattr(player, "schedule", {}) or {}
    count = 0
    for game in schedule.values():
        if isinstance(game, dict):
            game_date_value = game.get("date")
        else:
            game_date_value = getattr(game, "date", None)
        game_date = as_date(game_date_value)
        if game_date is not None and today <= game_date <= sunday:
            count += 1
    return count


def numeric(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def player_average(player: object) -> float:
    projected = numeric(getattr(player, "projected_avg_points", None))
    actual = numeric(getattr(player, "avg_points", None))
    return projected if projected > 0 else actual


def candidate_from_player(player: object, today: date, sunday: date) -> PlayerCandidate:
    average = player_average(player)
    games = player_games_left(player, today, sunday)
    injury = str(getattr(player, "injuryStatus", "UNKNOWN") or "UNKNOWN")
    if bool(getattr(player, "injured", False)) and injury.upper() in {"", "ACTIVE", "UNKNOWN"}:
        injury = "INJURED"
    return PlayerCandidate(
        name=str(getattr(player, "name", "Unknown")),
        position=str(getattr(player, "position", "—")),
        nba_team=str(getattr(player, "proTeam", "—")),
        injury=injury,
        avg_points=average,
        games_left=games,
        projected_week_points=average * games,
    )



def next_week_bounds(today: date) -> tuple[date, date]:
    days_until_monday = 7 - today.weekday()
    next_monday = today + timedelta(days=days_until_monday)
    return next_monday, next_monday + timedelta(days=6)


def player_game_dates(player: object, start: date, end: date) -> list[date]:
    schedule = getattr(player, "schedule", {}) or {}
    dates = []
    for game in schedule.values():
        if isinstance(game, dict):
            game_date_value = game.get("date")
        else:
            game_date_value = getattr(game, "date", None)
        game_date = as_date(game_date_value)
        if game_date is not None and start <= game_date <= end:
            dates.append(game_date)
    return sorted(set(dates))


def recent_player_average(player: object, period: str) -> float | None:
    stats = getattr(player, "stats", {}) or {}
    year = getattr(player, "year", None)
    keys = []
    if year is not None:
        keys.append(f"{year}_{period}")
    keys.extend(key for key in stats if str(key).endswith(f"_{period}"))
    for key in keys:
        split = stats.get(key)
        if not isinstance(split, dict):
            continue
        value = numeric(split.get("applied_avg", split.get("avg")), -1.0)
        if value > 0:
            return value
    return None


def injury_availability_multiplier(
    player: object, game_dates: list[date]
) -> tuple[str, date | None, float]:
    status = str(getattr(player, "injuryStatus", "UNKNOWN") or "UNKNOWN").upper()
    status = status.replace(" ", "_")
    injured = bool(getattr(player, "injured", False))
    expected_return = as_date(getattr(player, "expected_return_date", None))

    status_factors = {
        "ACTIVE": 1.0,
        "PROBABLE": 0.95,
        "QUESTIONABLE": 0.75,
        "DAY_TO_DAY": 0.70,
        "DOUBTFUL": 0.35,
        "OUT": 0.0,
        "INJURY_RESERVE": 0.0,
        "INJURED_RESERVE": 0.0,
        "IR": 0.0,
        "INACTIVE": 0.0,
        "SUSPENDED": 0.0,
    }
    factor = status_factors.get(status, 0.65 if injured else 0.90)

    if expected_return is not None and game_dates:
        games_after_return = sum(game_date >= expected_return for game_date in game_dates)
        return_fraction = games_after_return / len(game_dates)
        if status in {"OUT", "INJURY_RESERVE", "INJURED_RESERVE", "IR"}:
            factor = return_fraction * 0.90
        else:
            factor = min(factor, return_fraction)
    elif status in {"OUT", "INJURY_RESERVE", "INJURED_RESERVE", "IR", "INACTIVE", "SUSPENDED"}:
        factor = 0.0
    elif injured and status in {"", "UNKNOWN", "ACTIVE"}:
        factor = min(factor, 0.65)

    return status, expected_return, max(0.0, min(1.0, factor))


def blended_player_average(
    projected: float,
    season: float,
    last_7: float | None,
    last_15: float | None,
    last_30: float | None,
) -> float:
    components = [
        (projected, 0.35),
        (season, 0.30),
        (last_30 or 0.0, 0.15),
        (last_15 or 0.0, 0.10),
        (last_7 or 0.0, 0.10),
    ]
    available = [(value, weight) for value, weight in components if value > 0]
    if not available:
        return 0.0
    total_weight = sum(weight for _, weight in available)
    return sum(value * weight for value, weight in available) / total_weight


def latest_player_news(player: object) -> str:
    news = getattr(player, "news", []) or []
    for item in news:
        if isinstance(item, dict):
            headline = str(item.get("headline", "") or "").strip()
            if headline:
                return headline.replace("|", "/")
    return ""


def outlook_candidate_from_player(
    player: object, start: date, end: date
) -> OutlookCandidate:
    game_dates = player_game_dates(player, start, end)
    season_avg = numeric(getattr(player, "avg_points", None))
    projected_avg = numeric(getattr(player, "projected_avg_points", None))
    last_7 = recent_player_average(player, "last_7")
    last_15 = recent_player_average(player, "last_15")
    last_30 = recent_player_average(player, "last_30")
    blended_avg = blended_player_average(
        projected_avg, season_avg, last_7, last_15, last_30
    )
    status, expected_return, injury_factor = injury_availability_multiplier(
        player, game_dates
    )
    percent_owned_value = numeric(getattr(player, "percent_owned", None), -1.0)
    percent_started_value = numeric(getattr(player, "percent_started", None), -1.0)

    return OutlookCandidate(
        name=str(getattr(player, "name", "Unknown")),
        position=str(getattr(player, "position", "—")),
        nba_team=str(getattr(player, "proTeam", "—")),
        injury=status,
        expected_return=expected_return,
        game_dates=game_dates,
        season_avg=season_avg,
        projected_avg=projected_avg,
        last_7=last_7,
        last_15=last_15,
        last_30=last_30,
        blended_avg=blended_avg,
        injury_multiplier=injury_factor,
        estimated_points=blended_avg * len(game_dates) * injury_factor,
        percent_owned=percent_owned_value if percent_owned_value >= 0 else None,
        percent_started=percent_started_value if percent_started_value >= 0 else None,
        latest_news=latest_player_news(player),
    )


def display_average(value: float | None) -> str:
    return f"{value:.1f}" if value is not None and value > 0 else "—"


def render_next_week_outlook(
    team: object, free_agents: list[object], today: date
) -> str:
    start, end = next_week_bounds(today)
    roster_ids = {
        getattr(player, "playerId", None) for player in getattr(team, "roster", [])
    }
    candidates = []
    for player in free_agents:
        if getattr(player, "playerId", None) in roster_ids:
            continue
        candidate = outlook_candidate_from_player(player, start, end)
        if candidate.game_dates and candidate.blended_avg > 0 and candidate.estimated_points > 0:
            candidates.append(candidate)

    candidates.sort(
        key=lambda item: (
            item.estimated_points,
            item.blended_avg,
            len(item.game_dates),
            item.percent_started or 0.0,
        ),
        reverse=True,
    )

    lines = [
        f"## Next-week waiver outlook ({start.strftime('%b %-d')}–{end.strftime('%b %-d')})",
        "",
        "Top three free-agent priorities for the upcoming Monday–Sunday scoring week. "
        "The estimate blends ESPN projected and season FPPG with recent 30/15/7-day "
        "averages, multiplies by scheduled games, and discounts for reported injury risk.",
        "",
        "| Priority | Player | Injury | Games (dates) | Season FPPG | L7 | L15 | L30 | ESPN proj. FPPG | Est. points* | Owned |",
        "|---:|---|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]

    if not candidates:
        lines.append("| — | No qualifying free agents found in the returned pool | | | | | | | | | |")
    else:
        for index, item in enumerate(candidates[:3], start=1):
            game_text = f"{len(item.game_dates)} ({', '.join(d.strftime('%a %-d') for d in item.game_dates)})"
            owned_text = f"{item.percent_owned:.1f}%" if item.percent_owned is not None else "—"
            lines.append(
                f"| {index} | **{item.name}** ({item.position}, {item.nba_team}) "
                f"| {item.injury} | {game_text} | {item.season_avg:.1f} "
                f"| {display_average(item.last_7)} | {display_average(item.last_15)} "
                f"| {display_average(item.last_30)} | {item.projected_avg:.1f} "
                f"| **{item.estimated_points:.1f}** | {owned_text} |"
            )

        lines.extend(["", "### Pickup priorities and context", ""])
        for index, item in enumerate(candidates[:3], start=1):
            game_text = ", ".join(d.strftime("%a %-d") for d in item.game_dates)
            details = (
                f"{len(item.game_dates)} games ({game_text}); blended estimate "
                f"{item.blended_avg:.1f} FPPG before injury adjustment; "
                f"{item.estimated_points:.1f} estimated points after a "
                f"{item.injury_multiplier:.0%} availability factor."
            )
            if item.last_7 is not None and item.season_avg > 0:
                trend = item.last_7 - item.season_avg
                details += f" Last-7-day average is {trend:+.1f} vs. season average."
            if item.expected_return is not None:
                details += f" ESPN expected return: {item.expected_return.strftime('%b %-d')}."
            if item.percent_started is not None:
                details += f" Started in {item.percent_started:.1f}% of leagues."
            lines.append(f"{index}. **{item.name}:** {details}")
            if item.latest_news:
                lines.append(f"   - Latest ESPN news: {item.latest_news}")

    lines.extend([
        "",
        "*Est. points are directional, not guaranteed. L7/L15/L30 are ESPN recent-window "
        "per-game averages; 'ESPN proj. FPPG' is ESPN's season projection. Injury factors "
        "are conservative heuristics, not medical predictions. Injury status and news "
        "reflect the data returned by ESPN at report time. The scan is limited to the "
        f"top {FREE_AGENT_POOL_SIZE} free agents returned by ESPN, so deeper options may be missed.*",
    ])
    return "\n".join(lines)

def render_report(league: League, today: date) -> str:
    monday, sunday = week_bounds(today)
    team = next((item for item in league.teams if int(item.team_id) == TEAM_ID), None)
    if team is None:
        available_teams = ", ".join(
            f"{item.team_id}: {item.team_name}" for item in league.teams
        )
        raise RuntimeError(f"Team ID {TEAM_ID} not found. Teams: {available_teams}")

    roster = [
        candidate_from_player(player, today, sunday) for player in team.roster
    ]
    roster_ids = {getattr(player, "playerId", None) for player in team.roster}
    free_agents = league.free_agents(size=FREE_AGENT_POOL_SIZE)

    available = []
    for player in free_agents:
        if getattr(player, "playerId", None) in roster_ids:
            continue
        candidate = candidate_from_player(player, today, sunday)
        if candidate.games_left > 0 and candidate.avg_points > 0:
            available.append(candidate)

    available.sort(
        key=lambda item: (item.projected_week_points, item.avg_points, item.games_left),
        reverse=True,
    )
    roster_with_games = [item for item in roster if item.games_left > 0]
    roster_with_games.sort(key=lambda item: item.projected_week_points)

    lines = [
        f"# ESPN Fantasy Basketball — {TEAM_NAME}",
        "",
        f"**Report date:** {today.strftime('%A, %B %-d, %Y')}  ",
        f"**Scoring week:** {monday.strftime('%b %-d')}–{sunday.strftime('%b %-d')}  ",
        f"**League:** 14 teams · **Weekly add limit:** {MAX_WEEKLY_ADDS}",
        "",
        "## Best available players for the rest of this week",
        "",
        "| Player | Pos | NBA | Injury | FPPG* | Games left | Est. remaining points |",
        "|---|---:|---:|---|---:|---:|---:|",
    ]

    if not available:
        lines.append("| No qualifying free agents found | | | | | | |")
    else:
        for item in available[:15]:
            lines.append(
                f"| {item.name} | {item.position} | {item.nba_team} | {item.injury} "
                f"| {item.avg_points:.1f} | {item.games_left} "
                f"| {item.projected_week_points:.1f} |"
            )

    lines.extend([
        "",
        "## Add/drop targets",
        "",
        "Comparison-based suggestions only; no transactions are submitted. "
        "Each option compares a waiver player's estimated remaining-week points "
        "with a lower-value player currently on your roster.",
        "",
        "| Add | Est. points | Consider dropping | Est. points | Net estimate |",
        "|---|---:|---|---:|---:|",
    ])

    comparisons = []
    for free_agent in available:
        for roster_player in roster_with_games:
            net = free_agent.projected_week_points - roster_player.projected_week_points
            if net > 0:
                comparisons.append((net, free_agent, roster_player))
    comparisons.sort(key=lambda item: item[0], reverse=True)

    used_adds = set()
    used_drops = set()
    shown = 0
    for net, add_player, drop_player in comparisons:
        if add_player.name in used_adds or drop_player.name in used_drops:
            continue
        lines.append(
            f"| **{add_player.name}** ({add_player.games_left} GP) "
            f"| {add_player.projected_week_points:.1f} "
            f"| {drop_player.name} ({drop_player.games_left} GP) "
            f"| {drop_player.projected_week_points:.1f} | **+{net:.1f}** |"
        )
        used_adds.add(add_player.name)
        used_drops.add(drop_player.name)
        shown += 1
        if shown >= MAX_WEEKLY_ADDS:
            break
    if shown == 0:
        lines.append("| No clear positive-value add/drop found | | | | |")

    lines.extend(["", "## Roster watch", ""])
    if roster_with_games:
        for item in roster_with_games[:8]:
            lines.append(
                f"- **{item.name}** — {item.games_left} games left, "
                f"{item.avg_points:.1f} projected FPPG, {item.injury}."
            )
    else:
        lines.append("- No roster players with a remaining game were identified.")

    if today.weekday() in (5, 6):
        lines.extend(["", render_next_week_outlook(team, free_agents, today)])

    lines.extend([
        "",
        "*FPPG = ESPN fantasy points per game (season projection when available; "
        "otherwise season average). Remaining points are FPPG × scheduled games, "
        "not a guarantee. Injury designations may lag breaking news.*",
        "",
        "**Read-only:** verify injury news, lineup status, and remaining weekly "
        "acquisitions in ESPN before acting. This tool never adds or drops players.",
    ])
    return "\n".join(lines)


def main() -> int:
    espn_s2 = os.environ.get("ESPN_S2")
    swid = os.environ.get("ESPN_SWID")
    if not espn_s2 or not swid:
        raise RuntimeError("Missing ESPN_S2 or ESPN_SWID GitHub Actions secret.")

    today = datetime.now(EASTERN).date()
    league = League(
        league_id=LEAGUE_ID,
        year=season_start_year(today),
        espn_s2=espn_s2,
        swid=swid,
    )
    report = render_report(league, today)
    print(report)

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as summary_file:
            summary_file.write(report + "\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Report failed: {exc}", file=sys.stderr)
        raise
