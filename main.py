"""Daily ESPN fantasy basketball waiver report. Read-only; never submits transactions."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from espn_api.basketball import League
from espn_api.basketball.player import Player

EASTERN = ZoneInfo("America/New_York")
LEAGUE_ID = 1850516159
TEAM_ID = 12
TEAM_NAME = "Scottie on the Spectrum"
MAX_WEEKLY_ADDS = 5
FREE_AGENT_POOL_SIZE = 500


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
    game_probabilities: dict[date, float]
    season_avg: float
    projected_avg: float
    last_7: float | None
    last_15: float | None
    last_30: float | None
    blended_avg: float
    injury_multiplier: float
    expected_games: float
    estimated_points: float
    percent_owned: float | None
    percent_started: float | None
    latest_news: str
    pos_rank: int | None
    data_sources: int
    confidence: str
    lineup_slot: str


def hydrate_free_agents(
    league: League, players: list[object], batch_size: int = 40
) -> list[object]:
    """Rebuild free-agent Player objects with schedule and recent stat splits.

    espn-api's League.free_agents() constructs Player(player, year) without the
    NBA pro schedule. Its player-card endpoint can attach the league's full
    schedule and request ESPN's last_7/last_15/last_30 splits in the same batch.
    """
    originals = {
        getattr(player, "playerId", None): player
        for player in players
        if getattr(player, "playerId", None) is not None
    }
    player_ids = sorted(originals)
    hydrated: dict[int, object] = {}
    additional_filters = [
        f"01{league.year}",
        f"02{league.year}",
        f"03{league.year}",
    ]

    for offset in range(0, len(player_ids), batch_size):
        batch = player_ids[offset:offset + batch_size]
        response = league.espn_request.get_player_card(
            batch,
            league.finalScoringPeriod,
            additional_filters=additional_filters,
        )
        wraps = response.get("players", []) if isinstance(response, dict) else []
        for wrap in wraps:
            if not isinstance(wrap, dict):
                continue
            player = Player(wrap, league.year, league.pro_schedule)
            hydrated[player.playerId] = player

    # Preserve ESPN's free-agent ordering; leave any missing card unhydrated so
    # schedule coverage checks can detect incomplete player-card responses.
    return [hydrated.get(player_id, originals[player_id]) for player_id in player_ids]



def load_lineup_configuration(league: League) -> tuple[list[str], bool, str]:
    """Return active lineup slots, daily-lock mode, and a human-readable source."""
    slot_map = {
        0: "PG", 1: "SG", 2: "SF", 3: "PF", 4: "C", 5: "G", 6: "F",
        7: "SG/SF", 8: "G/F", 9: "PF/C", 10: "F/C", 11: "UT",
        12: "BE", 13: "IR", 14: "", 15: "Rookie",
    }
    try:
        response = league.espn_request.league_get(params={"view": "mSettings"})
        settings = response.get("settings", response) if isinstance(response, dict) else {}
        roster_settings = settings.get("rosterSettings", {}) if isinstance(settings, dict) else {}
        counts = roster_settings.get("lineupSlotCounts", {})
        slots: list[str] = []
        for raw_slot, raw_count in counts.items():
            try:
                slot_id = int(raw_slot)
                slot = slot_map.get(slot_id, str(raw_slot))
            except (TypeError, ValueError):
                slot = str(raw_slot)
            count = max(0, int(raw_count or 0))
            if slot not in {"", "BE", "IR", "Rookie"}:
                slots.extend([slot] * count)
        if not slots:
            return [], True, "ESPN active-slot settings unavailable; raw weekly totals fallback"
        lock_type = str(roster_settings.get("lineupLocktimeType", "INDIVIDUAL_GAME")).upper()
        daily_lineups = lock_type != "SCORING_PERIOD"
        mode = "daily game-time locks" if daily_lineups else "scoring-period lock"
        return slots, daily_lineups, mode
    except Exception as exc:
        return [], True, f"lineup settings unavailable ({type(exc).__name__}); raw weekly totals fallback"


def season_start_year(today: date) -> int:
    # ESPN basketball season IDs use the year the season ends (e.g. 2026–27 = 2027).
    return today.year + 1 if today.month >= 10 else today.year


def week_bounds(today: date) -> tuple[date, date]:
    monday = today - timedelta(days=today.weekday())
    return monday, monday + timedelta(days=6)


def as_date(value: object) -> date | None:
    if isinstance(value, datetime):
        # espn-api uses datetime.fromtimestamp(), which creates a naive datetime
        # in the runner's local timezone. Convert to ET before taking the date;
        # otherwise late NBA games can shift to the next UTC calendar day.
        return value.astimezone(EASTERN).date()
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
    if year is not None:
        split = stats.get(f"{year}_{period}")
        if isinstance(split, dict):
            value = numeric(split.get("applied_avg"), -1.0)
            if value > 0:
                return value

    # The library keys recent splits as <season>_last_7, _last_15, and _last_30.
    for key, split in stats.items():
        if str(key).endswith(f"_{period}") and isinstance(split, dict):
            value = numeric(split.get("applied_avg"), -1.0)
            if value > 0:
                return value
    return None

def game_play_probability(player: object, game_date: date) -> float:
    status = str(getattr(player, "injuryStatus", "UNKNOWN") or "UNKNOWN").upper()
    status = status.replace("-", "_").replace(" ", "_")
    injured = bool(getattr(player, "injured", False))
    expected_return = as_date(getattr(player, "expected_return_date", None))

    if expected_return is not None and game_date < expected_return:
        return 0.0
    if status in {"INACTIVE", "SUSPENDED"}:
        return 0.0
    if status in {"OUT", "INJURY_RESERVE", "INJURED_RESERVE", "IR"}:
        # A published return date is uncertain; allow only a discounted chance
        # for games on/after that date.
        return 0.55 if expected_return is not None else 0.0
    risk_factors = {
        "ACTIVE": 1.0,
        "PROBABLE": 0.95,
        "QUESTIONABLE": 0.75,
        "DAY_TO_DAY": 0.70,
        "DOUBTFUL": 0.30,
    }
    if status in risk_factors:
        return risk_factors[status]
    return 0.60 if injured else 1.0


def expected_playable_games(
    player: object, game_dates: list[date]
) -> tuple[str, date | None, float, float, dict[date, float]]:
    status = str(getattr(player, "injuryStatus", "UNKNOWN") or "UNKNOWN").upper()
    status = status.replace("-", "_").replace(" ", "_")
    expected_return = as_date(getattr(player, "expected_return_date", None))
    probabilities = {
        game_date: game_play_probability(player, game_date)
        for game_date in game_dates
    }
    expected_games = sum(probabilities.values())
    multiplier = expected_games / len(game_dates) if game_dates else 0.0
    return status, expected_return, multiplier, expected_games, probabilities


def blended_player_average(
    projected: float,
    season: float,
    last_7: float | None,
    last_15: float | None,
    last_30: float | None,
) -> float:
    """Use ESPN's league-specific projection first; fall back without requiring every split."""
    if projected > 0:
        return projected
    if season > 0:
        return season
    for value in (last_30, last_15, last_7):
        if value is not None and value > 0:
            return value
    return 0.0


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
    model_avg = blended_player_average(
        projected_avg, season_avg, last_7, last_15, last_30
    )
    status, expected_return, injury_factor, expected_games, game_probabilities = expected_playable_games(
        player, game_dates
    )
    percent_owned_value = numeric(getattr(player, "percent_owned", None), -1.0)
    percent_started_value = numeric(getattr(player, "percent_started", None), -1.0)
    pos_rank_value = numeric(getattr(player, "posRank", None), -1.0)

    data_sources = sum(
        value > 0
        for value in (
            projected_avg, season_avg, last_7 or 0.0,
            last_15 or 0.0, last_30 or 0.0,
        )
    )
    schedule_present = bool(getattr(player, "schedule", None))
    if data_sources >= 3 and schedule_present and status != "UNKNOWN":
        confidence = "High"
    elif data_sources >= 2 and schedule_present:
        confidence = "Medium"
    else:
        confidence = "Low"

    return OutlookCandidate(
        name=str(getattr(player, "name", "Unknown")),
        position=str(getattr(player, "position", "—")),
        nba_team=str(getattr(player, "proTeam", "—")),
        injury=status,
        expected_return=expected_return,
        game_dates=game_dates,
        game_probabilities=game_probabilities,
        season_avg=season_avg,
        projected_avg=projected_avg,
        last_7=last_7,
        last_15=last_15,
        last_30=last_30,
        blended_avg=model_avg,
        injury_multiplier=injury_factor,
        expected_games=expected_games,
        estimated_points=model_avg * expected_games,
        percent_owned=percent_owned_value if percent_owned_value >= 0 else None,
        percent_started=percent_started_value if percent_started_value >= 0 else None,
        latest_news=latest_player_news(player),
        pos_rank=int(pos_rank_value) if pos_rank_value > 0 else None,
        data_sources=data_sources,
        confidence=confidence,
        lineup_slot=str(getattr(player, "lineupSlot", "") or "").upper(),
    )


def display_average(value: float | None) -> str:
    return f"{value:.1f}" if value is not None and value > 0 else "—"


def drop_priority_key(candidate: OutlookCandidate) -> tuple[float, float, float]:
    return (
        candidate.estimated_points,
        candidate.blended_avg,
        candidate.expected_games,
    )


def swap_priority_key(
    comparison: tuple[float, OutlookCandidate, OutlookCandidate]
) -> tuple[float, float, float, float]:
    net_gain, add_player, _ = comparison
    rank_tiebreaker = -(add_player.pos_rank or 9999)
    return (
        net_gain,
        add_player.estimated_points,
        add_player.expected_games,
        rank_tiebreaker,
    )


def player_can_fill_slot(player: object, slot: str) -> bool:
    eligible = {
        str(position).upper()
        for position in (getattr(player, "eligibleSlots", []) or [])
    }
    primary = str(getattr(player, "position", "") or "").upper()
    if not eligible and primary:
        eligible.add(primary)
    if slot == "UT":
        return True
    if slot == "G":
        return bool(eligible.intersection({"PG", "SG", "G"}))
    if slot == "F":
        return bool(eligible.intersection({"SF", "PF", "F"}))
    if "/" in slot:
        return slot in eligible or bool(eligible.intersection(slot.split("/")))
    return slot in eligible


def best_fit_points(
    players: list[object], player_scores: dict[int, float], slots: list[str]
) -> float:
    """Maximize active lineup points with positional eligibility and slot limits."""
    slot_holders: dict[int, int] = {}
    player_by_id = {
        getattr(player, "playerId", None): player
        for player in players
        if getattr(player, "playerId", None) in player_scores
    }

    def place(player_id: int, seen_slots: set[int]) -> bool:
        player = player_by_id[player_id]
        for index, slot in enumerate(slots):
            if index in seen_slots or not player_can_fill_slot(player, slot):
                continue
            seen_slots.add(index)
            previous = slot_holders.get(index)
            if previous is None or place(previous, seen_slots):
                slot_holders[index] = player_id
                return True
        return False

    ordered_ids = sorted(
        player_by_id,
        key=lambda player_id: player_scores[player_id],
        reverse=True,
    )
    for player_id in ordered_ids:
        place(player_id, set())
    return sum(player_scores[player_id] for player_id in set(slot_holders.values()))


def lineup_week_points(
    roster: list[object],
    candidates: dict[int, OutlookCandidate],
    slots: list[str],
    daily_lineups: bool,
) -> float:
    """Project only points that can fit in active lineup slots."""
    if not slots:
        return sum(
            candidates[getattr(player, "playerId")].estimated_points
            for player in roster
            if getattr(player, "playerId", None) in candidates
            and candidates[getattr(player, "playerId")].lineup_slot != "IR"
        )

    if not daily_lineups:
        scores = {
            getattr(player, "playerId"): candidates[getattr(player, "playerId")].estimated_points
            for player in roster
            if getattr(player, "playerId", None) in candidates
            and candidates[getattr(player, "playerId")].lineup_slot != "IR"
        }
        return best_fit_points(roster, scores, slots)

    dates = sorted({
        game_date
        for player in roster
        if getattr(player, "playerId", None) in candidates
        for game_date in candidates[getattr(player, "playerId")].game_dates
    })
    total = 0.0
    for game_date in dates:
        daily_scores = {}
        for player in roster:
            player_id = getattr(player, "playerId", None)
            candidate = candidates.get(player_id)
            if candidate is None or candidate.lineup_slot == "IR":
                continue
            probability = candidate.game_probabilities.get(game_date, 0.0)
            if probability > 0 and candidate.blended_avg > 0:
                daily_scores[player_id] = candidate.blended_avg * probability
        total += best_fit_points(roster, daily_scores, slots)
    return total


def _best_swap(
    roster: list[object],
    candidate_players: list[object],
    candidate_data: dict[int, OutlookCandidate],
    base_points: float,
    slots: list[str],
    daily_lineups: bool,
    excluded_add_ids: set[int],
) -> tuple[float, object, object, float] | None:
    best = None
    best_key = None
    roster_ids = {getattr(player, "playerId", None) for player in roster}
    droppable = [
        player for player in roster
        if candidate_data.get(getattr(player, "playerId", None)) is not None
        and candidate_data[getattr(player, "playerId")].lineup_slot != "IR"
    ]

    for add_player in candidate_players:
        add_id = getattr(add_player, "playerId", None)
        if add_id in roster_ids or add_id in excluded_add_ids or add_id not in candidate_data:
            continue
        add_data = candidate_data[add_id]
        if add_data.expected_games <= 0 or add_data.blended_avg <= 0:
            continue
        for drop_player in droppable:
            drop_id = getattr(drop_player, "playerId", None)
            hypothetical = [player for player in roster if getattr(player, "playerId", None) != drop_id]
            hypothetical.append(add_player)
            after_points = lineup_week_points(
                hypothetical, candidate_data, slots, daily_lineups
            )
            gain = after_points - base_points
            key = (
                gain,
                add_data.estimated_points,
                add_data.expected_games,
                -(add_data.pos_rank or 9999),
            )
            if best_key is None or key > best_key:
                best_key = key
                best = (gain, add_player, drop_player, after_points)
    return best


def render_next_week_outlook(
    league: League, team: object, free_agents: list[object], today: date
) -> str:
    start, end = next_week_bounds(today)
    roster = list(getattr(team, "roster", []) or [])
    roster_candidates = {
        getattr(player, "playerId", None): outlook_candidate_from_player(player, start, end)
        for player in roster
        if getattr(player, "playerId", None) is not None
    }
    roster_ids = set(roster_candidates)

    schedule_records = sum(
        bool(getattr(player, "schedule", None))
        for player in free_agents
        if str(getattr(player, "proTeam", "")) not in {"FA", "0", ""}
    )
    nba_players = sum(
        1 for player in free_agents
        if str(getattr(player, "proTeam", "")) not in {"FA", "0", ""}
    )
    schedule_pct = 100.0 * schedule_records / nba_players if nba_players else 0.0

    slots, daily_lineups, lineup_mode = load_lineup_configuration(league)
    active_roster = [
        player for player in roster
        if roster_candidates.get(getattr(player, "playerId", None)) is not None
        and roster_candidates[getattr(player, "playerId")].lineup_slot != "IR"
    ]
    lines = [
        f"## Next-week roster upgrades ({start.strftime('%b %-d')}–{end.strftime('%b %-d')})",
        "",
        "Pickups are ranked by the change they make to your projected **starting-lineup total**, "
        "not by free-agent points per game alone. The model respects eligible positions, active "
        "roster-slot limits, the NBA schedule, and ESPN injury availability.",
        "",
        f"**Lineup model:** {len(slots)} active slots · {lineup_mode}. "
        + ("Daily best-lineup optimization." if daily_lineups else "One best lineup across the scoring period."),
        "",
        f"**Schedule hydration:** {schedule_records}/{nba_players} rostered-NBA free agents "
        f"({schedule_pct:.0f}%) have schedule data.",
    ]

    if not roster_candidates:
        lines.extend(["", "Could not compare pickups because the roster data is empty."])
        return "\n".join(lines)

    if nba_players and schedule_pct < 80:
        lines.extend([
            "",
            "**Data warning:** ESPN player-card schedule coverage is too low for trustworthy "
            "waiver rankings. The report stopped rather than treating missing schedules as zero games. "
            "Check the workflow log and ESPN player-card responses before acting.",
        ])
        return "\n".join(lines)

    candidate_players = []
    candidate_data = dict(roster_candidates)
    for player in free_agents:
        player_id = getattr(player, "playerId", None)
        if player_id is None or player_id in roster_ids:
            continue
        candidate = outlook_candidate_from_player(player, start, end)
        if candidate.expected_games <= 0 or candidate.blended_avg <= 0:
            continue
        candidate_data[player_id] = candidate
        candidate_players.append(player)

    if not candidate_players:
        lines.extend([
            "",
            "No free agents in the returned ESPN pool have both a scheduled game next week "
            "and a usable points estimate.",
        ])
        return "\n".join(lines)

    baseline = lineup_week_points(active_roster, candidate_data, slots, daily_lineups)
    virtual_roster = list(active_roster)
    selected = []
    used_add_ids: set[int] = set()

    # Greedily simulate a bundle of up to three distinct pickups. Each subsequent move
    # is evaluated against the roster after the prior hypothetical move, so the same
    # weak roster slot cannot be counted as three separate upgrades.
    for _ in range(3):
        best = _best_swap(
            virtual_roster, candidate_players, candidate_data, baseline,
            slots, daily_lineups, used_add_ids,
        )
        if best is None or best[0] <= 0.05:
            break
        gain, add_player, drop_player, after_points = best
        selected.append((gain, add_player, drop_player, after_points, True))
        virtual_roster = [
            player for player in virtual_roster
            if getattr(player, "playerId", None) != getattr(drop_player, "playerId", None)
        ]
        virtual_roster.append(add_player)
        used_add_ids.add(getattr(add_player, "playerId"))
        baseline = after_points

    # If fewer than three positive moves exist, show the strongest remaining options as
    # watchlist alternatives. They are ranked by marginal lineup impact, not called upgrades.
    while len(selected) < 3:
        best = _best_swap(
            virtual_roster, candidate_players, candidate_data, baseline,
            slots, daily_lineups, used_add_ids,
        )
        if best is None:
            break
        gain, add_player, drop_player, after_points = best
        selected.append((gain, add_player, drop_player, after_points, False))
        used_add_ids.add(getattr(add_player, "playerId"))
        candidate_players = [
            player for player in candidate_players
            if getattr(player, "playerId", None) not in used_add_ids
        ]

    if not selected:
        lines.extend(["", "No eligible positive roster improvements were identified."])
        return "\n".join(lines)

    news_ids = [
        getattr(add_player, "playerId", None)
        for _, add_player, _, _, _ in selected
        if getattr(add_player, "playerId", None) is not None
    ]
    news_by_id = {}
    if news_ids:
        try:
            news_result = league.player_info(playerId=news_ids, include_news=True)
            news_players = news_result if isinstance(news_result, list) else [news_result]
            news_by_id = {
                getattr(player, "playerId", None): player
                for player in news_players if player is not None
            }
        except Exception:
            news_by_id = {}

    lines.extend([
        "",
        "| Priority | Pickup | Consider dropping | Pickup est. pts | Drop est. pts | Team lineup gain | Games (expected) | Model FPPG | L15 | Injury | Confidence |",
        "|---:|---|---|---:|---:|---:|---:|---:|---|---|---|",
    ])

    for index, (gain, add_player, drop_player, after_points, is_upgrade) in enumerate(selected, start=1):
        add = candidate_data[getattr(add_player, "playerId")]
        drop = candidate_data[getattr(drop_player, "playerId")]
        label = f"**{add.name}**" if is_upgrade else f"{add.name} (watchlist)"
        marker = "+" if gain >= 0 else ""
        lines.append(
            f"| {index} | {label} ({add.position}, {add.nba_team}) "
            f"| {drop.name} | {add.estimated_points:.1f} | {drop.estimated_points:.1f} "
            f"| **{marker}{gain:.1f}** | {len(add.game_dates)} ({add.expected_games:.1f}) "
            f"| {display_average(add.blended_avg)} | {display_average(add.last_15)} "
            f"| {add.injury} | {add.confidence} |"
        )

    positive_count = sum(1 for item in selected if item[4])
    lines.extend(["", "### Why these pickups rank highest", ""])
    if positive_count:
        lines.append(
            f"The model found {positive_count} positive roster upgrade(s). Later gains are "
            "incremental estimates after simulating earlier positive pickups."
        )
    else:
        lines.append(
            "No available player is projected to improve the optimized lineup total. The "
            "options below are the closest alternatives, shown as a watchlist rather than "
            "mislabelled as upgrades."
        )

    for index, (gain, add_player, drop_player, after_points, is_upgrade) in enumerate(selected, start=1):
        add = candidate_data[getattr(add_player, "playerId")]
        drop = candidate_data[getattr(drop_player, "playerId")]
        game_text = ", ".join(
            game_date.strftime("%a %-d") for game_date in add.game_dates
        ) or "schedule unavailable"
        trend = ""
        if add.last_15 is not None and add.season_avg > 0:
            change = add.last_15 - add.season_avg
            trend = f" L15 is {change:+.1f} FPPG versus season average."
        details = (
            f"{'Adds' if is_upgrade else 'Would add'} **{gain:+.1f} projected team points** "
            f"over {drop.name} after lineup-slot optimization. {len(add.game_dates)} scheduled "
            f"games ({game_text}); {add.expected_games:.1f} expected playable games. "
            f"Model FPPG {display_average(add.blended_avg)} "
            f"(ESPN projection {display_average(add.projected_avg)}, "
            f"season {display_average(add.season_avg)}, "
            f"L7 {display_average(add.last_7)}, "
            f"L15 {display_average(add.last_15)}, "
            f"L30 {display_average(add.last_30)}).{trend}"
        )
        if add.pos_rank is not None:
            details += f" ESPN position rank: {add.pos_rank}."
        if add.expected_return is not None:
            details += f" ESPN expected return: {add.expected_return.strftime('%b %-d')}."
        lines.append(f"{index}. **{add.name}:** {details}")
        news_player = news_by_id.get(getattr(add_player, "playerId", None))
        news = latest_player_news(news_player) if news_player is not None else ""
        if news:
            lines.append(f"   - Latest ESPN news: {news}")

    lines.extend([
        "",
        "*Model FPPG uses ESPN's league-specific projected FPPG when present, then season average; "
        "recent 7/15/30-day splits are fallback inputs and trend context, not hard eligibility rules. "
        "Injury availability is estimated per scheduled game. Daily leagues optimize each date's "
        "active slots; scoring-period-lock leagues optimize one weekly lineup. The scan covers up "
        f"to {FREE_AGENT_POOL_SIZE} ESPN free agents.*",
        "",
        "*This is a short-term points-league estimate. Role changes, late injury news, and actual "
        "lineup decisions can change the result; no roster transactions are submitted.*",
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
    raw_free_agents = league.free_agents(size=FREE_AGENT_POOL_SIZE)
    free_agents = hydrate_free_agents(league, raw_free_agents)

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
        lines.extend(["", render_next_week_outlook(league, team, free_agents, today)])

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
