# ESPN Fantasy Basketball Assistant

Daily, read-only waiver analysis for **Scottie on the Spectrum** (ESPN league ID `1850516159`, team ID `12`).

## What it does

- Runs daily at approximately **9:45 AM Eastern** using GitHub Actions.
- Evaluates the remaining Monday–Sunday scoring week.
- Ranks available players by estimated remaining-week fantasy points.
- Compares waiver options against lower-value players on your roster.
- Includes position, NBA team, ESPN injury designation, fantasy points per game, and remaining games.
- Displays no more than **5 add/drop comparisons**, matching the league's weekly move limit.
- On **Saturday and Sunday**, compares waiver pickups directly with the weakest projected player on your roster for the upcoming week and ranks up to 3 positive net upgrades. It includes scheduled games, season/recent 7/15/30-day FPPG, ESPN projections, injury/return-date adjustments, position rank, ownership, news, and a data-confidence label. If fewer than 3 positive upgrades exist, it says so and shows the closest alternatives separately.
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

On Saturdays and Sundays, the next-week outlook compares each returned free agent against the roster player with the lowest estimated points for that week. It ranks by net gain (pickup estimate minus drop-candidate estimate), rather than by a free agent's standalone points total. It uses the same FPPG blend: ESPN projected FPPG (35%), season FPPG (30%), recent 30-day average (15%), recent 15-day average (10%), and recent 7-day average (10%). Missing averages are omitted and remaining weights normalized, so a player does not need every data field to be considered. It then estimates scheduled games and applies an injury/expected-return adjustment. Positional rank helps break ties; ownership, start rate, recent trends, and ESPN news are shown as context. The report labels data confidence based on available stat splits and schedule data. The free-agent scan was increased to 500 players for the deep league. Players already occupying an IR lineup slot are not proposed as drop candidates.

This is a weekly-streaming heuristic, not a full model of minutes, role changes, opponent difficulty, or individualized injury probabilities. Injury designations and news can lag. The waiver pool is limited to the top 250 players returned by ESPN's API.

If the league uses category scoring rather than points scoring, FPPG is not an appropriate decision metric and should be replaced with category-specific projections.
