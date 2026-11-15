# ESPN Fantasy Basketball Assistant

Daily, read-only waiver analysis for **Scottie on the Spectrum** (ESPN league ID `1850516159`, team ID `12`).

## What it does

- Runs daily at approximately **9:45 AM Eastern** using GitHub Actions.
- Evaluates the remaining Monday–Sunday scoring week.
- Ranks available players by estimated remaining-week fantasy points.
- Compares waiver options against lower-value players on your roster.
- Includes position, NBA team, ESPN injury designation, fantasy points per game, and remaining games.
- Displays no more than **5 add/drop comparisons**, matching the league's weekly move limit.
- On **Saturday and Sunday**, adds a next-week outlook with the top 3 waiver priorities, scheduled game dates, season and recent 7/15/30-day FPPG averages, ESPN projected FPPG, injury status/expected return date, ownership, and available ESPN player news.
- Publishes the report in the GitHub Actions run summary.

**It never executes roster transactions.** Check ESPN for late injury news, starting status, and your actual remaining acquisition count.

## Required GitHub Actions secrets

In **Settings → Secrets and variables → Actions → Secrets**, add:

- `ESPN_S2`
- `ESPN_SWID`

Do not put ESPN cookies in repository Variables, source code, workflow YAML, issues, or commit history. Rotate credentials that were previously exposed in chat.

## Run it manually

Open **Actions → Daily ESPN Fantasy Basketball Report → Run workflow**. The result appears in the run's summary.

## Schedule and daylight saving time

GitHub Actions cron uses UTC and may start late. The workflow schedules both 13:45 and 14:45 UTC, then checks the New York local clock so only the 9:45 AM Eastern run proceeds. GitHub does not guarantee execution at the exact minute.

## Methodology and limitations

The script uses the unofficial `espn-api` Python library to read ESPN Fantasy Basketball league, roster, free-agent, schedule, projection, recent-average, injury, and player-news data.

For the current week, estimated remaining points are:

`FPPG × scheduled NBA games remaining this week`

On Saturdays and Sundays, the next-week outlook blends ESPN projected FPPG (35%), season FPPG (30%), recent 30-day average (15%), recent 15-day average (10%), and recent 7-day average (10%). Missing averages are omitted and the remaining weights are normalized. The blended FPPG is multiplied by next week's scheduled games and a conservative availability factor based on ESPN injury status and expected return date. The report also shows recent averages, ESPN's projected FPPG, ownership, and available player-news headlines.

This is a weekly-streaming heuristic, not a full model of minutes, role changes, opponent difficulty, or individualized injury probabilities. Injury designations and news can lag. The waiver pool is limited to the top 250 players returned by ESPN's API.

If the league uses category scoring rather than points scoring, FPPG is not an appropriate decision metric and should be replaced with category-specific projections.
