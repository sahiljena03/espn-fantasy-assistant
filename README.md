# ESPN Fantasy Basketball Assistant

Daily, read-only waiver analysis for **Scottie on the Spectrum** (ESPN league ID `1850516159`, team ID `12`).

## What it does

- Runs daily at approximately **9:45 AM Eastern** using GitHub Actions.
- Evaluates the remaining Monday–Sunday scoring week.
- Ranks available players by estimated remaining-week fantasy points.
- Compares waiver options against lower-value players on your roster.
- Includes position, NBA team, ESPN injury designation, fantasy points per game, and remaining games.
- Displays no more than **5 add/drop comparisons**, matching the league's weekly move limit.
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

The script uses the unofficial `espn-api` Python library to read ESPN Fantasy Basketball league, roster, free-agent, schedule, and projection data. Estimated remaining points are:

`ESPN projected fantasy points per game × scheduled NBA games remaining this week`

If ESPN does not provide a positive projected average, the script falls back to the player's season average. This is a weekly-streaming heuristic, not a full model of minutes, role changes, matchup difficulty, category scarcity, or injury probabilities. Injury designations can lag news. The waiver pool is limited to the top 250 players returned by ESPN's API.

If the league uses category scoring rather than points scoring, FPPG is not an appropriate decision metric and should be replaced with category-specific projections.
