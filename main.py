"""Daily ESPN fantasy basketball waiver report. Read-only; never submits transactions."""
from __future__ import annotations

import json
import os
import re
import sys
from urllib.request import Request, urlopen
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import statistics
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
    game_log_games: int = 0
    season_games: int = 0
    season_median: float | None = None
    game_stddev: float | None = None
    coefficient_variation: float | None = None
    consistency_rate: float | None = None
    floor_20: float | None = None
    last_5: float | None = None
    season_minutes: float | None = None
    last_5_minutes: float | None = None
    prior_minutes: float | None = None
    base_fppg: float = 0.0
    role_uplift: float = 0.0
    opportunity_note: str = ""


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
        if isinstance(response, dict):
            wraps = response.get("players", [])
        elif isinstance(response, list):
            wraps = response
        else:
            wraps = []

        for wrap in wraps:
            if not isinstance(wrap, dict):
                continue
            player = Player(wrap, league.year, league.pro_schedule)
            metadata = player_metadata_from_wrapper(wrap)
            player.percent_owned = metadata["percent_owned"]
            player.percent_started = metadata["percent_started"]
            original = originals.get(player.playerId)
            if original is not None:
                player.lineupSlot = getattr(original, "lineupSlot", player.lineupSlot)
            hydrated[player.playerId] = player

        # Player.player_info returns ESPN's event-level scoring data. Merge it when
        # available so consistency/minutes metrics use realized games, not projections.
        try:
            detailed_result = league.player_info(playerId=batch)
            detailed_players = (
                detailed_result if isinstance(detailed_result, list)
                else [detailed_result]
            )
            for detailed in detailed_players:
                player_id = getattr(detailed, "playerId", None)
                current = hydrated.get(player_id)
                if current is None:
                    continue
                detailed_stats = getattr(detailed, "stats", {}) or {}
                if detailed_stats:
                    current.stats.update(detailed_stats)
                detailed_schedule = getattr(detailed, "schedule", {}) or {}
                if not getattr(current, "schedule", None) and detailed_schedule:
                    current.schedule = detailed_schedule
                detailed_average = numeric(getattr(detailed, "avg_points", None), 0.0)
                if detailed_average > 0:
                    current.avg_points = detailed_average
                    current.total_points = numeric(
                        getattr(detailed, "total_points", None),
                        getattr(current, "total_points", 0.0),
                    )
        except Exception:
            # Some league/library versions do not expose batch player_info; the
            # season average remains usable, but the report will mark log confidence lower.
            pass

    # Preserve ESPN's original ordering; leave missing cards unhydrated so validation
    # can fail closed rather than inventing a player's schedule or production.
    return [hydrated.get(player_id, originals[player_id]) for player_id in player_ids]


def player_metadata_from_wrapper(wrap: dict) -> dict[str, float | None]:
    """Extract ownership fields omitted by espn-api's basketball Player model."""
    pool_entry = wrap.get("playerPoolEntry", {})
    if not isinstance(pool_entry, dict):
        pool_entry = {}
    inner = pool_entry.get("player", wrap.get("player", {}))
    if not isinstance(inner, dict):
        inner = {}
    ownership = inner.get("ownership", wrap.get("ownership", {}))
    if not isinstance(ownership, dict):
        ownership = {}
    owned = numeric(ownership.get("percentOwned"), -1.0)
    started = numeric(ownership.get("percentStarted"), -1.0)
    return {
        "percent_owned": owned if owned >= 0 else None,
        "percent_started": started if started >= 0 else None,
    }



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
    """Blend projections with realized production to limit early-season projection spikes."""
    components = [
        (projected, 0.40),
        (season, 0.30),
        (last_30 or 0.0, 0.15),
        (last_15 or 0.0, 0.10),
        (last_7 or 0.0, 0.05),
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


ESPN_NBA_INJURY_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries"
ESPN_NBA_NEWS_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/news?limit=100"
ESPN_NBA_INJURY_PAGE = "https://www.espn.com/nba/injuries"
ESPN_NBA_NEWS_PAGE = "https://www.espn.com/nba/"


def fetch_espn_json(url: str, timeout: int = 8) -> dict:
    request = Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; FantasyWaiverReport/1.0)"},
    )
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def normalize_player_name(value: object) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", str(value or "").lower()).strip()


def parse_espn_injury_signals(payload: dict) -> list[dict]:
    """Parse relevant absences from ESPN's league-wide NBA injury report."""
    signals = []
    relevant_statuses = {
        "OUT", "INACTIVE", "DOUBTFUL", "QUESTIONABLE", "DAY-TO-DAY",
        "DAY TO DAY", "INJURY RESERVE", "INJURED RESERVE", "IR",
    }
    normalized_relevant = {item.replace("-", " ") for item in relevant_statuses}
    for team_record in payload.get("injuries", []) or []:
        team = team_record.get("team", {}) or {}
        team_id = str(team.get("id", "") or "")
        team_abbr = str(team.get("abbreviation", "") or "").upper()
        for injury in team_record.get("injuries", []) or []:
            athlete = injury.get("athlete", {}) or {}
            status = str(injury.get("status", "") or "").strip().upper()
            normalized_status = status.replace("_", " ").replace("-", " ")
            if normalized_status not in normalized_relevant:
                continue
            injury_type = injury.get("type", {}) or {}
            injury_detail = str(
                injury_type.get("name", "")
                or injury.get("details", "")
                or injury.get("shortComment", "")
            ).strip()
            signals.append({
                "player_id": str(athlete.get("id", "") or ""),
                "player_name": str(athlete.get("displayName", "") or ""),
                "team_id": team_id,
                "team_abbr": team_abbr,
                "position": "",
                "status": status,
                "detail": injury_detail,
                "signal_type": "ESPN injury report",
                "source_url": ESPN_NBA_INJURY_PAGE,
            })
    return signals


def parse_espn_news_signals(
    payload: dict,
    players_by_name: dict[str, list[object]],
    today: date,
) -> list[dict]:
    """Find recent NBA rest, absence, injury, and minutes-limit headlines."""
    trigger_pattern = re.compile(
        r"\b(rest(?:ing)?|load management|maintenance(?: day)?|will sit|sitting out|"
        r"ruled out|out tonight|out for|will miss|misses|sidelined|inactive|"
        r"minutes restriction|minutes limit|limited minutes|day-to-day|"
        r"questionable|doubtful)\b",
        re.IGNORECASE,
    )
    signals = []
    articles = payload.get("articles") or payload.get("headlines") or []
    player_names = sorted(players_by_name, key=len, reverse=True)
    for article in articles:
        headline = str(article.get("headline", "") or "").strip()
        description = str(article.get("description", "") or "").strip()
        article_text = f"{headline}. {description}"
        if not trigger_pattern.search(article_text):
            continue
        published = str(article.get("published", "") or "")
        if not published:
            continue
        try:
            published_date = datetime.fromisoformat(
                published.replace("Z", "+00:00")
            ).date()
        except ValueError:
            continue
        if (today - published_date).days > 10 or published_date > today + timedelta(days=1):
            continue
        normalized_article = normalize_player_name(article_text)
        mentioned_names = []
        for player_name in player_names:
            if len(player_name.split()) < 2:
                continue
            pattern = r"(?<![a-z0-9])" + re.escape(player_name) + r"(?![a-z0-9])"
            if re.search(pattern, normalized_article):
                mentioned_names.append(player_name)
        links = article.get("links", {}) or {}
        web_link = links.get("web", {}) or {}
        source_url = str(web_link.get("href", "") or ESPN_NBA_NEWS_PAGE)
        lowered = article_text.lower()
        if re.search(r"load management|rest(?:ing)?|maintenance", lowered):
            signal_status = "REST/LOAD-MANAGEMENT NEWS"
        elif re.search(r"minutes restriction|minutes limit|limited minutes", lowered):
            signal_status = "MINUTES-LIMIT NEWS"
        elif re.search(r"ruled out|out tonight|out for|will miss|to miss|misses|sidelined|inactive|will sit|sitting out", lowered):
            signal_status = "ABSENCE NEWS"
        else:
            signal_status = "INJURY-STATUS NEWS"
        for player_name in mentioned_names:
            matching_players = players_by_name.get(player_name, [])
            if len(matching_players) != 1:
                continue
            player = matching_players[0]
            player_id = str(getattr(player, "playerId", "") or "")
            team_abbr = str(getattr(player, "proTeam", "") or "").upper()
            position = str(getattr(player, "position", "") or "")
            if not player_id or not team_abbr:
                continue
            confirmed_absence = bool(re.search(
                r"\b(ruled out|will miss|to miss|out (?:tonight|today|for)|"
                r"sidelined|for at least \d+ weeks|at least \d+ weeks)\b",
                lowered,
            ))
            sustained_absence = bool(re.search(
                r"\b(for at least \d+ weeks|for \d+ weeks|at least \d+ weeks|"
                r"next \d+ games|rest of (?:the )?season|indefinitely)\b",
                lowered,
            ))
            signals.append({
                "confirmed_absence": confirmed_absence,
                "sustained_absence": sustained_absence,
                "player_id": player_id,
                "player_name": str(getattr(player, "name", player_name)),
                "team_id": "",
                "team_abbr": team_abbr,
                "position": position,
                "status": signal_status,
                "detail": headline or description,
                "signal_type": "Recent ESPN NBA news",
                "source_url": source_url,
            })
    return signals


def fetch_espn_team_positions(team_ids: set[str]) -> dict[str, dict[str, str]]:
    """Fetch NBA roster positions only when the league fantasy rosters lack an injured player."""
    positions: dict[str, dict[str, str]] = {}
    for team_id in sorted(team_ids):
        if not team_id:
            continue
        try:
            payload = fetch_espn_json(
                f"https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams/{team_id}/roster",
                timeout=5,
            )
        except Exception:
            continue
        athletes = payload.get("athletes", []) or []
        flattened = []
        for entry in athletes:
            if isinstance(entry, dict) and isinstance(entry.get("items"), list):
                flattened.extend(entry.get("items", []))
            elif isinstance(entry, dict):
                flattened.append(entry)
        for athlete in flattened:
            athlete_id = str(athlete.get("id", "") or "")
            position = athlete.get("position", {}) or {}
            abbreviation = str(position.get("abbreviation", "") or "")
            if athlete_id and abbreviation:
                positions[athlete_id] = {
                    "position": abbreviation,
                    "name": str(athlete.get("displayName", "") or ""),
                }
    return positions


def position_groups(value: object) -> set[str]:
    tokens = set(re.findall(r"PG|SG|SF|PF|C|G|F", str(value or "").upper()))
    if tokens.intersection({"PG", "SG"}):
        tokens.add("G")
    if tokens.intersection({"SF", "PF"}):
        tokens.add("F")
    return tokens


def minutes_opportunity_watch(
    free_agents: list[object],
    candidate_players: list[object],
    candidate_data: dict[int, OutlookCandidate],
    all_roster_players: list[object],
    today: date,
) -> str:
    """Flag low-started free agents who may benefit from same-team absences."""
    lines = [
        "### Injury and minutes-opportunity watch",
        "",
        f"Sources: [ESPN NBA injury report]({ESPN_NBA_INJURY_PAGE}) · "
        f"[ESPN NBA news]({ESPN_NBA_NEWS_PAGE}).",
    ]
    catalog_by_id = {}
    players_by_name: dict[str, list[object]] = {}
    for player in list(all_roster_players) + list(free_agents):
        player_id = str(getattr(player, "playerId", "") or "")
        if player_id:
            catalog_by_id.setdefault(player_id, player)
        normalized_name = normalize_player_name(getattr(player, "name", ""))
        if normalized_name:
            existing = players_by_name.setdefault(normalized_name, [])
            if all(
                getattr(item, "playerId", None) != getattr(player, "playerId", None)
                for item in existing
            ):
                existing.append(player)

    injury_signals = []
    news_signals = []
    injury_available = False
    news_available = False
    error_messages = []
    try:
        injury_payload = fetch_espn_json(ESPN_NBA_INJURY_URL)
        injury_signals = parse_espn_injury_signals(injury_payload)
        injury_available = True
    except Exception as exc:
        error_messages.append(f"injury report unavailable ({type(exc).__name__})")
    try:
        news_payload = fetch_espn_json(ESPN_NBA_NEWS_URL)
        news_signals = parse_espn_news_signals(news_payload, players_by_name, today)
        news_available = True
    except Exception as exc:
        error_messages.append(f"NBA news unavailable ({type(exc).__name__})")

    for signal in injury_signals:
        player = catalog_by_id.get(signal["player_id"])
        if player is not None:
            signal["position"] = str(getattr(player, "position", "") or "")
            if not signal["team_abbr"]:
                signal["team_abbr"] = str(getattr(player, "proTeam", "") or "").upper()
    missing_position_team_ids = {
        signal["team_id"]
        for signal in injury_signals
        if signal["team_id"] and not position_groups(signal["position"])
    }
    team_positions = fetch_espn_team_positions(missing_position_team_ids)
    for signal in injury_signals:
        position_record = team_positions.get(signal["player_id"], {})
        if not signal["position"]:
            signal["position"] = position_record.get("position", "")

    signals = injury_signals + news_signals
    if not injury_available and not news_available:
        lines.extend([
            "",
            "The scan could not retrieve either ESPN source. No injury-driven minutes opportunity is being assumed.",
            f"Source status: {'; '.join(error_messages)}.",
            "",
            "*No role or fantasy-point boosts are applied from this scan.*",
        ])
        return "\n".join(lines)

    opportunity_by_player: dict[int, dict] = {}
    for signal in signals:
        team_abbr = str(signal.get("team_abbr", "") or "").upper()
        affected_groups = position_groups(signal.get("position", ""))
        if not team_abbr or not affected_groups:
            continue
        status = str(signal.get("status", "") or "")
        status_upper = status.upper()
        if status_upper in {"OUT", "INACTIVE", "INJURY RESERVE", "INJURED RESERVE", "IR"}:
            status_weight = 3.0
        elif status_upper == "ABSENCE NEWS":
            status_weight = 2.0
        elif "DOUBTFUL" in status_upper:
            status_weight = 2.5
        elif "REST/LOAD-MANAGEMENT" in status_upper:
            status_weight = 2.0
        elif "QUESTIONABLE" in status_upper or "DAY-TO-DAY" in status_upper:
            status_weight = 1.5
        elif "MINUTES-LIMIT" in status_upper:
            status_weight = 1.25
        else:
            status_weight = 1.0

        for candidate_player in candidate_players:
            player_id = getattr(candidate_player, "playerId", None)
            candidate = candidate_data.get(player_id)
            if candidate is None or str(getattr(candidate_player, "proTeam", "") or "").upper() != team_abbr:
                continue
            candidate_groups = position_groups(candidate.position)
            shared_groups = affected_groups.intersection(candidate_groups)
            if not shared_groups:
                continue
            if candidate.percent_started is not None and candidate.percent_started > 45.0:
                continue
            candidate_primary = str(candidate.position).upper()
            affected_primary = str(signal.get("position", "") or "").upper()
            position_match = 1.0 if candidate_primary == affected_primary else 0.5
            started = candidate.percent_started
            bench_bonus = 0.5 if started is not None and started <= 15.0 else 0.0
            score = status_weight + position_match + bench_bonus
            record = opportunity_by_player.setdefault(player_id, {
                "candidate": candidate,
                "score": 0.0,
                "reasons": [],
                "sources": [],
                "confirmed_absence": False,
                "sustained_absence": False,
            })
            if (
                status_upper in {"OUT", "INACTIVE", "INJURY RESERVE", "INJURED RESERVE", "IR"}
                or signal.get("confirmed_absence", False)
            ):
                record["confirmed_absence"] = True
            if (
                status_upper in {"INJURY RESERVE", "INJURED RESERVE", "IR"}
                or signal.get("sustained_absence", False)
            ):
                record["sustained_absence"] = True
            detail = str(signal.get("detail", "") or "").replace("|", "/").strip()
            detail_text = f", {detail[:72]}" if detail else ""
            reason = (
                f"{signal['player_name']} ({status}{detail_text}) at {team_abbr}, "
                f"same position group"
            )
            if reason not in record["reasons"]:
                record["reasons"].append(reason)
                record["score"] += score
            source = signal.get("source_url", "")
            if source and source not in record["sources"]:
                record["sources"].append(source)

    # Do not invent minutes from a headline. Apply a small, explicitly bounded
    # role uplift only when a confirmed teammate absence coincides with an observed
    # increase in the free agent's own recent minutes.
    for item in opportunity_by_player.values():
        candidate = item["candidate"]
        if (
            item["sustained_absence"]
            and candidate.game_log_games >= 8
            and candidate.prior_minutes is not None
            and candidate.last_5_minutes is not None
            and candidate.base_fppg > 0
        ):
            minutes_change = candidate.last_5_minutes - candidate.prior_minutes
            minutes_growth = (
                candidate.last_5_minutes / candidate.prior_minutes - 1.0
                if candidate.prior_minutes > 0 else 0.0
            )
            if minutes_change >= 3.0 and minutes_growth >= 0.15:
                candidate.role_uplift = min(
                    0.06, max(0.02, 0.02 + (minutes_growth - 0.15) * 0.08)
                )
                candidate.opportunity_note = (
                    f"Observed L5 minutes {candidate.last_5_minutes:.1f} vs "
                    f"{candidate.prior_minutes:.1f} before the recent confirmed absence"
                )
                candidate.blended_avg = (
                    candidate.base_fppg * (1.0 + candidate.role_uplift)
                )
                candidate.estimated_points = (
                    candidate.blended_avg * candidate.expected_games
                )
                item["role_uplift_applied"] = True
            else:
                item["role_uplift_applied"] = False
        else:
            item["role_uplift_applied"] = False

    if not opportunity_by_player:
        lines.extend([
            "",
            "No verified same-team, same-position-group free-agent beneficiaries were found in the current waiver pool.",
            f"Source check: injury report {'available' if injury_available else 'unavailable'}; "
            f"NBA news {'available' if news_available else 'unavailable'}.",
        ])
    else:
        opportunities = list(opportunity_by_player.values())
        def opportunity_sort_key(item: dict) -> tuple[float, float, float]:
            candidate = item["candidate"]
            minutes_growth = (
                candidate.last_5_minutes / candidate.prior_minutes - 1.0
                if candidate.last_5_minutes is not None
                and candidate.prior_minutes is not None
                and candidate.prior_minutes > 0 else 0.0
            )
            return (
                item["score"],
                candidate.role_uplift,
                minutes_growth,
                candidate.estimated_points,
                candidate.last_15 or 0.0,
            )
        opportunities.sort(key=opportunity_sort_key, reverse=True)
        lines.extend([
            "",
            "| Potential beneficiary | Team / Pos | Teammate availability signal | Fantasy started % | L5 minutes vs prior | Role adjustment |",
            "|---|---|---|---:|---:|---:|",
        ])
        for item in opportunities[:5]:
            candidate = item["candidate"]
            reasons = "; ".join(item["reasons"][:2]).replace("|", "/")
            started_label = (
                f"{candidate.percent_started:.1f}%"
                if candidate.percent_started is not None else "Unknown"
            )
            minutes_trend = "—"
            if candidate.last_5_minutes is not None and candidate.prior_minutes is not None:
                minutes_trend = (
                    f"{candidate.last_5_minutes:.1f} / {candidate.prior_minutes:.1f}"
                )
            uplift_label = (
                f"+{candidate.role_uplift * 100:.1f}%"
                if candidate.role_uplift > 0 else "Watch only"
            )
            lines.append(
                f"| **{candidate.name}** | {candidate.nba_team} / {candidate.position} "
                f"| {reasons} | {started_label} | {minutes_trend} | {uplift_label} |"
            )
        lines.append("")
        lines.append(
            "*Same-team/position matching is a watchlist signal, not proof of starter succession. "
            "No extra minutes or fantasy points are assumed unless a teammate has a sustained absence "
            "(injury reserve or explicit multi-week news) and the candidate's last-five game minutes "
            "are at least 15% and 3 minutes above the pre-L5 baseline; only then is a +2% to +6% FPPG "
            "role adjustment applied. A single-game OUT or questionable tag remains watchlist-only.*"
        )
        source_urls = []
        for item in opportunities[:5]:
            for source in item["sources"]:
                if source not in source_urls:
                    source_urls.append(source)
        if source_urls:
            lines.append("Sources for flagged players: " + " · ".join(
                f"[ESPN update {index}]({url})"
                for index, url in enumerate(source_urls[:5], start=1)
            ) + ".")
    if error_messages:
        lines.extend(["", "Partial-source warning: " + "; ".join(error_messages) + "."])
    return "\n".join(lines)


def player_game_log_records(
    player: object, as_of: date
) -> list[dict[str, float | date | None]]:
    """Return realized, league-scoring game totals matched to ESPN schedule IDs."""
    stats = getattr(player, "stats", {}) or {}
    schedule = getattr(player, "schedule", {}) or {}
    records: list[dict[str, float | date | None]] = []
    if not isinstance(stats, dict) or not isinstance(schedule, dict):
        return records

    for game_id, game in schedule.items():
        stat = stats.get(str(game_id), stats.get(game_id))
        if not isinstance(stat, dict):
            continue
        points = numeric(stat.get("applied_total"), -1.0)
        if points < 0:
            continue
        game_date = as_date(stat.get("date"))
        if game_date is None:
            game_date = as_date(
                game.get("date") if isinstance(game, dict)
                else getattr(game, "date", None)
            )
        if game_date is None or game_date > as_of:
            continue

        totals = stat.get("total")
        if not isinstance(totals, dict):
            totals = {}
        minutes: float | None = None
        for key, value in totals.items():
            normalized = re.sub(r"[^a-z]", "", str(key).lower())
            if normalized in {"min", "mins", "minutes"}:
                if isinstance(value, str) and ":" in value:
                    pieces = value.split(":", 1)
                    try:
                        minutes = float(pieces[0]) + float(pieces[1]) / 60.0
                    except ValueError:
                        minutes = None
                else:
                    parsed = numeric(value, -1.0)
                    minutes = parsed if parsed >= 0 else None
                break
        # A zero-minute record is a DNP and must not depress scoring consistency.
        if minutes is not None and minutes <= 0:
            continue
        records.append({
            "date": game_date,
            "points": points,
            "minutes": minutes,
        })

    records.sort(key=lambda item: item["date"] or date.min)
    return records


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = max(0.0, min(1.0, percentile)) * (len(ordered) - 1)
    lower = int(position)
    upper = min(len(ordered) - 1, lower + 1)
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def player_production_profile(
    player: object, as_of: date
) -> dict[str, float | int | None]:
    """Build a projection-free production/consistency profile from actual season results."""
    records = player_game_log_records(player, as_of)
    game_points = [float(item["points"]) for item in records]
    season_avg = numeric(getattr(player, "avg_points", None), 0.0)
    if season_avg <= 0 and game_points:
        season_avg = statistics.fmean(game_points)

    median = statistics.median(game_points) if game_points else None
    stddev = statistics.pstdev(game_points) if len(game_points) >= 2 else None
    coefficient = (
        stddev / abs(statistics.fmean(game_points))
        if stddev is not None and game_points and statistics.fmean(game_points) > 0
        else None
    )
    consistency = (
        sum(value >= season_avg * 0.75 for value in game_points) / len(game_points)
        if game_points and season_avg > 0 else None
    )
    floor_20 = _percentile(game_points, 0.20)
    last_5_values = game_points[-5:]
    last_5 = statistics.fmean(last_5_values) if last_5_values else None

    minute_values = [
        float(item["minutes"])
        for item in records if item["minutes"] is not None
    ]
    season_minutes = statistics.fmean(minute_values) if minute_values else None
    last_5_minutes_values = [
        float(item["minutes"])
        for item in records[-5:] if item["minutes"] is not None
    ]
    last_5_minutes = (
        statistics.fmean(last_5_minutes_values) if last_5_minutes_values else None
    )
    prior_minute_values = [
        float(item["minutes"])
        for item in records[:-5] if item["minutes"] is not None
    ]
    prior_minutes = (
        statistics.fmean(prior_minute_values)
        if len(prior_minute_values) >= 3 else None
    )

    # ESPN's appliedAverage is the current-season realized points-per-game value.
    # Robust game-log statistics temper small-sample spikes without importing projections.
    base_fppg = season_avg
    if season_avg > 0 and median is not None:
        if len(game_points) >= 5:
            shrunk_last_5 = (
                sum(last_5_values) + 5.0 * season_avg
            ) / (len(last_5_values) + 5.0)
            base_fppg = (
                0.70 * season_avg
                + 0.20 * median
                + 0.10 * shrunk_last_5
            )
        elif len(game_points) >= 3:
            base_fppg = 0.80 * season_avg + 0.20 * median

    volatility_penalty = 0.0
    if coefficient is not None:
        volatility_penalty = min(0.08, max(0.0, coefficient - 0.45) * 0.10)
    consistency_adjustment = 0.0
    if consistency is not None:
        consistency_adjustment = max(
            -0.035, min(0.02, (consistency - 0.60) * 0.07)
        )
    reliability_fppg = (
        base_fppg * (1.0 - volatility_penalty)
        * (1.0 + consistency_adjustment)
        if base_fppg > 0 else 0.0
    )

    total_points = numeric(getattr(player, "total_points", None), 0.0)
    inferred_games = (
        int(round(total_points / season_avg))
        if total_points > 0 and season_avg > 0 else 0
    )
    season_games = max(len(records), inferred_games)
    return {
        "season_avg": season_avg,
        "season_games": season_games,
        "game_log_games": len(records),
        "median": median,
        "stddev": stddev,
        "coefficient_variation": coefficient,
        "consistency_rate": consistency,
        "floor_20": floor_20,
        "last_5": last_5,
        "season_minutes": season_minutes,
        "last_5_minutes": last_5_minutes,
        "prior_minutes": prior_minutes,
        "base_fppg": base_fppg,
        "reliability_fppg": reliability_fppg,
    }


def outlook_candidate_from_player(
    player: object,
    start: date,
    end: date,
    as_of: date | None = None,
) -> OutlookCandidate:
    as_of = as_of or datetime.now(EASTERN).date()
    game_dates = player_game_dates(player, start, end)
    profile = player_production_profile(player, as_of)
    season_avg = float(profile["season_avg"] or 0.0)
    # Retained for diagnostics only. Projected FPPG is intentionally not used.
    projected_avg = numeric(getattr(player, "projected_avg_points", None))
    last_7 = recent_player_average(player, "last_7")
    last_15 = recent_player_average(player, "last_15")
    last_30 = recent_player_average(player, "last_30")
    model_avg = float(profile["reliability_fppg"] or 0.0)

    status, expected_return, injury_factor, expected_games, game_probabilities = expected_playable_games(
        player, game_dates
    )
    percent_owned_value = numeric(getattr(player, "percent_owned", None), -1.0)
    percent_started_value = numeric(getattr(player, "percent_started", None), -1.0)
    pos_rank_value = numeric(getattr(player, "posRank", None), -1.0)
    log_games = int(profile["game_log_games"] or 0)
    if log_games >= 10 and season_avg > 0 and status != "UNKNOWN":
        confidence = "High"
    elif log_games >= 5 and season_avg > 0:
        confidence = "Medium"
    elif season_avg > 0:
        confidence = "Low — limited game-log sample"
    else:
        confidence = "Low — missing actual scoring"

    candidate = OutlookCandidate(
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
        data_sources=sum([
            int(season_avg > 0),
            int(log_games > 0),
            int(last_7 is not None),
            int(last_15 is not None),
            int(last_30 is not None),
        ]),
        confidence=confidence,
        lineup_slot=str(getattr(player, "lineupSlot", "") or "").upper(),
        game_log_games=log_games,
        season_games=int(profile["season_games"] or 0),
        season_median=profile["median"],
        game_stddev=profile["stddev"],
        coefficient_variation=profile["coefficient_variation"],
        consistency_rate=profile["consistency_rate"],
        floor_20=profile["floor_20"],
        last_5=profile["last_5"],
        season_minutes=profile["season_minutes"],
        last_5_minutes=profile["last_5_minutes"],
        prior_minutes=profile["prior_minutes"],
        base_fppg=float(profile["base_fppg"] or 0.0),
    )
    return candidate

def display_average(value: float | None) -> str:
    return f"{value:.1f}" if value is not None and value > 0 else "—"


def is_protected_roster_player(candidate: OutlookCandidate) -> bool:
    """Protect only players whose actual-season production data is unavailable."""
    return candidate.blended_avg <= 0 or candidate.season_avg <= 0


def free_agent_rejection_reason(candidate: OutlookCandidate) -> str | None:
    """No arbitrary FPPG ceiling or ownership cutoff; require actual data and games."""
    if candidate.blended_avg <= 0 or candidate.season_avg <= 0:
        return "no actual season scoring"
    if not candidate.game_dates:
        return "no scheduled games"
    return None


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
    raw_eligible = getattr(player, "eligibleSlots", None)
    if raw_eligible is None:
        primary = str(getattr(player, "position", "") or "").upper()
        eligible = {primary} if primary else set()
    else:
        eligible = {
            str(position).upper()
            for position in (raw_eligible or [])
        }

    # Utility eligibility is explicit in ESPN's eligibleSlots field. Do not assume
    # that every player can occupy a custom utility slot.
    if slot == "UT":
        return "UT" in eligible
    if slot in eligible:
        return True
    slot_groups = {
        "G": {"PG", "SG", "G"},
        "F": {"SF", "PF", "F"},
        "G/F": {"PG", "SG", "SF", "PF", "G", "F"},
        "SG/SF": {"SG", "SF"},
        "PF/C": {"PF", "C"},
        "F/C": {"SF", "PF", "F", "C"},
    }
    if slot in slot_groups:
        return bool(eligible.intersection(slot_groups[slot]))
    if "/" in slot:
        return bool(eligible.intersection(slot.split("/")))
    return False


def best_fit_assignments(
    players: list[object],
    player_scores: dict[int, float],
    slots: list[str],
) -> dict[int, str]:
    """Assign the highest-scoring eligible players to the available active slots."""
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
    return {
        player_id: slots[slot_index]
        for slot_index, player_id in slot_holders.items()
    }


def best_fit_points(
    players: list[object], player_scores: dict[int, float], slots: list[str]
) -> float:
    """Return the points assigned to active slots by the eligibility optimizer."""
    assignments = best_fit_assignments(players, player_scores, slots)
    return sum(player_scores[player_id] for player_id in assignments)


def lineup_assignments_by_date(
    roster: list[object],
    candidates: dict[int, OutlookCandidate],
    slots: list[str],
    daily_lineups: bool,
) -> dict[date, dict[int, str]]:
    """Return the ESPN-eligible active lineup assignment for each scheduled date."""
    dates = sorted({
        game_date
        for player in roster
        if getattr(player, "playerId", None) in candidates
        for game_date in candidates[getattr(player, "playerId")].game_dates
    })
    if not dates:
        return {}

    if not daily_lineups:
        scores = {
            getattr(player, "playerId"): candidates[getattr(player, "playerId")].estimated_points
            for player in roster
            if getattr(player, "playerId", None) in candidates
            and candidates[getattr(player, "playerId")].lineup_slot != "IR"
        }
        weekly_assignments = best_fit_assignments(roster, scores, slots)
        return {game_date: weekly_assignments for game_date in dates}

    assignments_by_date: dict[date, dict[int, str]] = {}
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
        assignments_by_date[game_date] = best_fit_assignments(
            roster, daily_scores, slots
        )
    return assignments_by_date


def pickup_lineup_game_usage(
    player_id: int,
    roster: list[object],
    candidates: dict[int, OutlookCandidate],
    slots: list[str],
    daily_lineups: bool,
) -> tuple[int, int]:
    """Return active-lineup games and expected-available scheduled games for a pickup."""
    candidate = candidates.get(player_id)
    if candidate is None:
        return 0, 0
    available_dates = [
        game_date
        for game_date, probability in candidate.game_probabilities.items()
        if probability > 0
    ]
    if not slots:
        return len(available_dates), len(available_dates)
    assignments = lineup_assignments_by_date(
        roster, candidates, slots, daily_lineups
    )
    active_games = sum(
        player_id in assignments.get(game_date, {})
        for game_date in available_dates
    )
    return active_games, len(available_dates)


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
    eligible_drop_ids: set[int] | None = None,
    lineup_blocked_add_ids: set[int] | None = None,
) -> tuple[float, object, object, float, int, int] | None:
    best = None
    best_key = None
    roster_ids = {getattr(player, "playerId", None) for player in roster}
    droppable = [
        player for player in roster
        if (eligible_drop_ids is None or getattr(player, "playerId", None) in eligible_drop_ids)
        and candidate_data.get(getattr(player, "playerId", None)) is not None
        and candidate_data[getattr(player, "playerId")].lineup_slot != "IR"
        and not is_protected_roster_player(candidate_data[getattr(player, "playerId")])
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
            active_games, available_games = pickup_lineup_game_usage(
                add_id, hypothetical, candidate_data, slots, daily_lineups
            )
            if available_games <= 0 or active_games < available_games:
                if lineup_blocked_add_ids is not None:
                    lineup_blocked_add_ids.add(add_id)
                continue
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
                best = (gain, add_player, drop_player, after_points, active_games, available_games)
    return best


def team_game_counts(
    players: list[object],
    start: date,
    end: date,
) -> dict[str, int]:
    """Count unique scheduled NBA games per team from ESPN player schedules."""
    games_by_team: dict[str, set[str]] = {}
    for player in players:
        team = str(getattr(player, "proTeam", "") or "").strip().upper()
        if team in {"", "FA", "0", "NONE"}:
            continue
        schedule = getattr(player, "schedule", {}) or {}
        if not isinstance(schedule, dict):
            continue
        team_games = games_by_team.setdefault(team, set())
        for game_id, game in schedule.items():
            if isinstance(game, dict):
                game_date_value = game.get("date")
            else:
                game_date_value = getattr(game, "date", None)
            game_date = as_date(game_date_value)
            if game_date is not None and start <= game_date <= end:
                team_games.add(str(game_id))
    return {
        team: len(game_ids)
        for team, game_ids in games_by_team.items()
        if game_ids
    }


def render_team_schedule_targets(
    players: list[object],
    start: date,
    end: date,
) -> str:
    """Highlight four-game teams to target and two-game teams to avoid."""
    counts = team_game_counts(players, start, end)
    lines = [
        "### Team schedule profile",
        "",
        f"**Scoring window:** {start.strftime('%b %-d')}–{end.strftime('%b %-d')}. "
        "Use the team schedule as a tiebreaker when streaming or choosing between similar players.",
    ]
    if not counts:
        lines.extend([
            "",
            "Team schedules unavailable; no target/avoid teams can be verified.",
        ])
        return "\n".join(lines)

    target_teams = sorted(team for team, games in counts.items() if games == 4)
    consider_teams = sorted(team for team, games in counts.items() if games == 3)
    avoid_teams = sorted(team for team, games in counts.items() if games == 2)
    lines.extend([
        "",
        "**Target (4 games):** "
        + (", ".join(f"{team} (4)" for team in target_teams) if target_teams else "None"),
        "",
        "**Consider (3 games):** "
        + (", ".join(f"{team} (3)" for team in consider_teams) if consider_teams else "None")
        + ". Three-game players remain eligible when actual-season production and marginal lineup gain justify them.",
        "",
        "**Avoid (2 games):** "
        + (", ".join(f"{team} (2)" for team in avoid_teams) if avoid_teams else "None"),
        "",
        "*Counts use unique scheduled NBA game IDs from ESPN player-card schedules. "
        "Four games are a schedule advantage, not a mandatory filter; three-game players are "
        "evaluated on actual-season production, available lineup capacity, and marginal lineup fit.*",
    ])
    return "\n".join(lines)


def render_daily_lineup_gaps(
    roster: list[object],
    candidates: dict[int, OutlookCandidate],
    slots: list[str],
    daily_lineups: bool,
    start: date,
    end: date,
) -> str:
    """Show the days with the most unused active slots in the current roster."""
    assignments = lineup_assignments_by_date(roster, candidates, slots, daily_lineups)
    scheduled_dates = sorted({
        game_date
        for candidate in candidates.values()
        for game_date in candidate.game_dates
        if start <= game_date <= end
    })
    lines = [
        "### Remaining-day lineup capacity",
        "",
        "| Day | Priority day | Roster players with a game | Active slots filled | Open active slots |",
        "|---|---|---:|---:|---:|",
    ]
    if not scheduled_dates:
        lines.append("| No scheduled games in this scoring window | — | — | — |")
        return "\n".join(lines)
    for game_date in scheduled_dates:
        roster_playing = sum(
            candidate.game_probabilities.get(game_date, 0.0) > 0
            and candidate.lineup_slot != "IR"
            for player in roster
            for candidate in [candidates.get(getattr(player, "playerId", None))]
            if candidate is not None
        )
        filled = len(assignments.get(game_date, {}))
        open_slots = max(0, len(slots) - filled)
        is_priority_day = game_date.weekday() in {0, 1, 3, 5, 6}
        lines.append(
            f"| {game_date.strftime('%A %b %-d')} | {'Yes' if is_priority_day else 'No'} "
            f"| {roster_playing} | {filled}/{len(slots)} | **{open_slots}** |"
        )
    if not daily_lineups:
        lines.extend([
            "",
            "*Your league uses a scoring-period lineup lock; the same weekly lineup assignment "
            "is used for each date, so daily open-slot counts are informational rather than "
            "a promise that lineups can be changed after the period locks.*",
        ])
    else:
        lines.extend([
            "",
            "*Open active slots are the clearest streaming opportunity: the optimizer rewards "
            "players whose real NBA game dates land on those days and who fit an eligible active slot.*",
        ])
    return "\n".join(lines)


def render_next_week_outlook(
    league: League,
    team: object,
    free_agents: list[object],
    today: date,
    roster_players: list[object] | None = None,
    scoring_window: tuple[date, date] | None = None,
    window_title: str = "Next-week roster upgrades",
) -> str:
    start, end = scoring_window or next_week_bounds(today)
    roster = list(roster_players if roster_players is not None else (getattr(team, "roster", []) or []))
    roster_candidates = {
        getattr(player, "playerId", None): outlook_candidate_from_player(player, start, end, as_of=today)
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
        f"## {window_title} ({start.strftime('%b %-d')}–{end.strftime('%b %-d')})",
        "",
        "Pickups are ranked by the change they make to your **actual-production starting-lineup total**. "
        "Projections are excluded. The model uses this season's realized ESPN fantasy points, "
        "game-log consistency, current availability, NBA schedule, and legal active-slot capacity.",
        "",
        f"**Lineup model:** {len(slots)} active slots · {lineup_mode}. "
        + ("Daily best-lineup optimization." if daily_lineups else "One best lineup across the scoring period."),
        "",
        f"**Schedule hydration:** {schedule_records}/{nba_players} rostered-NBA free agents "
        f"({schedule_pct:.0f}%) have schedule data.",
        "",
        render_team_schedule_targets(
            list(free_agents) + roster, start, end
        ),
    ]

    if not roster_candidates:
        lines.extend(["", "Could not compare pickups because the roster data is empty."])
        return "\n".join(lines)

    if not slots:
        lines.extend([
            "",
            "**Data warning:** ESPN active-slot settings and player eligibility are required to verify "
            "that a pickup can enter your lineup for every available game. No pickup recommendations "
            "were generated because lineup fit could not be verified.",
        ])
        return "\n".join(lines)

    missing_roster_rates = [
        candidate.name
        for candidate in roster_candidates.values()
        if candidate.lineup_slot != "IR" and candidate.blended_avg <= 0
    ]
    if missing_roster_rates:
        names = ", ".join(missing_roster_rates[:8])
        lines.extend([
            "",
            f"**Data warning:** weekly fantasy rates are missing for roster players: {names}. "
            "No add/drop recommendations were generated because the model cannot safely value "
            "your current lineup.",
        ])
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
    rejected = {
        "no actual season scoring": 0,
        "no scheduled games": 0,
    }
    for player in free_agents:
        player_id = getattr(player, "playerId", None)
        if player_id is None or player_id in roster_ids:
            continue
        candidate = outlook_candidate_from_player(player, start, end, as_of=today)
        reason = free_agent_rejection_reason(candidate)
        if reason:
            rejected[reason] = rejected.get(reason, 0) + 1
            continue
        candidate_data[player_id] = candidate
        candidate_players.append(player)

    all_league_roster = []
    for fantasy_team in league.teams:
        all_league_roster.extend(getattr(fantasy_team, "roster", []) or [])
    lines.extend([
        "",
        minutes_opportunity_watch(
            free_agents, candidate_players, candidate_data, all_league_roster, today
        ),
        "",
        f"**Data exclusions:** {rejected['no actual season scoring']} players lacked usable actual-season scoring; "
        f"{rejected['no scheduled games']} had no game in this scoring window. "
        "There is no projection-based FPPG ceiling, no ownership cutoff, and no fixed FPPG drop threshold. "
        "A drop is recommended only when the full legal-lineup comparison shows a positive gain.",
    ])

    if not candidate_players:
        lines.extend([
            "",
            "No free agents remain after the scoring and schedule filters. "
            "The report will not relax these rules to fill three recommendation slots.",
        ])
        return "\n".join(lines)

    lines.extend([
        "",
        render_daily_lineup_gaps(
            active_roster, candidate_data, slots, daily_lineups, start, end
        ),
    ])

    baseline = lineup_week_points(active_roster, candidate_data, slots, daily_lineups)
    virtual_roster = list(active_roster)
    selected = []
    used_add_ids: set[int] = set()
    lineup_blocked_add_ids: set[int] = set()

    # Greedily simulate a bundle of up to three distinct pickups. Each subsequent move
    # is evaluated against the roster after the prior hypothetical move, so the same
    # weak roster slot cannot be counted as three separate upgrades.
    for _ in range(3):
        best = _best_swap(
            virtual_roster, candidate_players, candidate_data, baseline,
            slots, daily_lineups, used_add_ids, roster_ids, lineup_blocked_add_ids,
        )
        if best is None or best[0] <= 0.05:
            break
        gain, add_player, drop_player, after_points, active_games, available_games = best
        selected.append((gain, add_player, drop_player, after_points, active_games, available_games, True))
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
            slots, daily_lineups, used_add_ids, roster_ids, lineup_blocked_add_ids,
        )
        if best is None:
            break
        gain, add_player, drop_player, after_points, active_games, available_games = best
        selected.append((gain, add_player, drop_player, after_points, active_games, available_games, False))
        used_add_ids.add(getattr(add_player, "playerId"))
        candidate_players = [
            player for player in candidate_players
            if getattr(player, "playerId", None) not in used_add_ids
        ]

    if not selected:
        eligible_drops = [
            candidate.name for candidate in roster_candidates.values()
            if candidate.lineup_slot != "IR" and not is_protected_roster_player(candidate)
        ]
        if not eligible_drops:
            lines.extend([
                "",
                "No roster player has sufficient actual-season scoring data to evaluate as a drop candidate.",
            ])
        else:
            lines.extend([
                "",
                "No pickup/drop pair was found that improves the actual-production lineup while allowing the pickup "
                "to use every expected-available game.",
            ])
        if lineup_blocked_add_ids:
            lines.append(
                f"{len(lineup_blocked_add_ids)} waiver candidates were excluded because at least one "
                "expected-available game would be benched by your active-slot limits."
            )
        return "\n".join(lines)

    news_ids = [
        getattr(add_player, "playerId", None)
        for _, add_player, _, _, _, _, _ in selected
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
        "| Priority | Pickup | Season FPPG | Model FPPG | Median | L5 | Consistency | CV | Games (expected) | Active fit | Drop | Team gain | Role signal | Confidence |",
        "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|---|---|",
    ])

    for index, (gain, add_player, drop_player, after_points, active_games, available_games, is_upgrade) in enumerate(selected, start=1):
        add = candidate_data[getattr(add_player, "playerId")]
        drop = candidate_data[getattr(drop_player, "playerId")]
        label = f"**{add.name}**" if is_upgrade else f"{add.name} (watchlist)"
        marker = "+" if gain >= 0 else ""
        consistency_label = (
            f"{add.consistency_rate * 100:.0f}%"
            if add.consistency_rate is not None else "—"
        )
        cv_label = (
            f"{add.coefficient_variation:.2f}"
            if add.coefficient_variation is not None else "—"
        )
        role_label = (
            f"+{add.role_uplift * 100:.0f}%"
            if add.role_uplift > 0 else "—"
        )
        lines.append(
            f"| {index} | {label} ({add.position}, {add.nba_team}) "
            f"| {display_average(add.season_avg)} | {display_average(add.blended_avg)} "
            f"| {display_average(add.season_median)} | {display_average(add.last_5)} "
            f"| {consistency_label} | {cv_label} "
            f"| {len(add.game_dates)} ({add.expected_games:.1f}) "
            f"| {active_games}/{available_games} (0 sit) "
            f"| {drop.name} ({display_average(drop.season_avg)} FPPG) "
            f"| **{marker}{gain:.1f}** | {role_label} | {add.confidence} |"
        )

    positive_count = sum(1 for item in selected if item[6])
    lines.extend(["", "### Why these pickups rank highest", ""])
    if positive_count:
        lines.append(
            f"The model found {positive_count} positive roster upgrade(s). Later gains are "
            "incremental actual-production estimates after simulating earlier positive pickups."
        )
    else:
        lines.append(
            "No available player improves the optimized actual-production lineup total. The "
            "options below are the closest alternatives, shown as a watchlist rather than "
            "mislabelled as upgrades."
        )

    lineup_blocked_count = len(lineup_blocked_add_ids.difference(used_add_ids))
    if lineup_blocked_count:
        lines.append(
            f"{lineup_blocked_count} additional waiver candidates were excluded because at least "
            "one expected-available game would force them onto the bench under your active-slot limits."
        )

    for index, (gain, add_player, drop_player, after_points, active_games, available_games, is_upgrade) in enumerate(selected, start=1):
        add = candidate_data[getattr(add_player, "playerId")]
        drop = candidate_data[getattr(drop_player, "playerId")]
        game_text = ", ".join(
            game_date.strftime("%a %-d") for game_date in add.game_dates
        ) or "schedule unavailable"
        trend = ""
        if add.last_15 is not None and add.season_avg > 0:
            change = add.last_15 - add.season_avg
            trend = f" L15 is {change:+.1f} FPPG versus season average."
        consistency_label = (
            f"{add.consistency_rate * 100:.0f}% of logged games at/above 75% of season FPPG"
            if add.consistency_rate is not None else "consistency unavailable"
        )
        cv_label = (
            f"{add.coefficient_variation:.2f}"
            if add.coefficient_variation is not None else "CV unavailable"
        )
        details = (
            f"{'Adds' if is_upgrade else 'Would add'} **{gain:+.1f} actual-production team points** "
            f"over {drop.name} after optimizing your legal active lineup. "
            f"{len(add.game_dates)} remaining scheduled games ({game_text}); "
            f"{add.expected_games:.1f} expected playable games. "
            f"Lineup fit: {active_games}/{available_games} expected-available games started, "
            "with no forced benching. "
            f"Season actual FPPG {display_average(add.season_avg)} across {add.season_games} games; "
            f"game-log sample {add.game_log_games}; robust model FPPG {display_average(add.blended_avg)}; "
            f"median {display_average(add.season_median)}, L5 {display_average(add.last_5)}, "
            f"20th-percentile floor {display_average(add.floor_20)}, "
            f"consistency {consistency_label}, CV {cv_label}. "
            f"Last-five minutes {display_average(add.last_5_minutes)} vs prior baseline "
            f"{display_average(add.prior_minutes)}.{trend}"
        )
        if add.role_uplift > 0 and add.opportunity_note:
            details += (
                f" Evidence-gated role adjustment +{add.role_uplift * 100:.1f}%: "
                f"{add.opportunity_note}."
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
        "*Projections are not used. The season baseline is ESPN's realized league-scoring FPPG. "
        "Where game logs are available, the model combines 70% season mean, 20% median, and 10% "
        "last-five scoring shrunk toward the season mean with a five-game prior; it then applies "
        "small bounded adjustments for coefficient of variation and the share of games reaching "
        "75% of season FPPG. Lower-tail 20th-percentile scoring and log sample size are reported "
        "for risk context. This is a transparent heuristic, not a trained predictive model. "
        "All candidates are tested against the day-by-day schedule and your exact ESPN eligible slots; "
        "a pickup is rejected when any expected-available game would be forced to the bench. "
        "The engine re-runs the optimal lineup after each hypothetical swap, and only original "
        "roster players can be proposed as drops. The scan covers up to "
        f"{FREE_AGENT_POOL_SIZE} ESPN free agents.*",
        "",
        "*The score is a season-production estimate, not a guaranteed future result. Injury-driven "
        "role adjustments are capped and require both confirmed teammate absences and observed "
        "recent minutes growth. Late news and lineup decisions can change the result; no transactions "
        "are submitted.*",
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

    original_roster = list(getattr(team, "roster", []) or [])
    roster = hydrate_free_agents(league, original_roster)
    roster_ids = {getattr(player, "playerId", None) for player in roster}
    raw_free_agents = league.free_agents(size=FREE_AGENT_POOL_SIZE)
    free_agents = hydrate_free_agents(league, raw_free_agents)

    lines = [
        f"# ESPN Fantasy Basketball — {TEAM_NAME}",
        "",
        f"**Report date:** {today.strftime('%A, %B %-d, %Y')}  ",
        f"**Scoring week:** {monday.strftime('%b %-d')}–{sunday.strftime('%b %-d')}  ",
        f"**Remaining analysis window:** {today.strftime('%a %b %-d')}–{sunday.strftime('%a %b %-d')}  ",
        f"**League:** 14 teams · **Weekly add limit:** {MAX_WEEKLY_ADDS}",
        "",
        "## Current-week waiver optimization",
        "",
        "This report uses realized 2026–27 fantasy production only. ESPN projected FPPG and projected "
        "weekly totals are excluded from ranking and add/drop valuation. A move is recommended only "
        "when it improves your legal active lineup and the pickup can be started in every remaining "
        "game it is expected to play.",
        "",
        render_next_week_outlook(
            league,
            team,
            free_agents,
            today,
            roster_players=roster,
            scoring_window=(today, sunday),
            window_title="Remaining-week add/drop recommendations",
        ),
    ]

    if today.weekday() in (5, 6):
        lines.extend([
            "",
            render_next_week_outlook(
                league,
                team,
                free_agents,
                today,
                roster_players=roster,
                scoring_window=next_week_bounds(today),
                window_title="Next-week add/drop outlook",
            ),
        ])

    lines.extend([
        "",
        "*The model is decision support, not a promise of future points. Always verify late injury "
        "news and actual lineup lock times in ESPN. No roster transactions are submitted.*",
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
