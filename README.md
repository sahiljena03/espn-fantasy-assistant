# ESPN Fantasy Basketball Assistant

Daily, read-only waiver analysis for **Scottie on the Spectrum** (ESPN league ID `1850516159`, team ID `12`).

## What it does

- Runs daily at approximately **9:45 AM Eastern** using GitHub Actions.
- Evaluates the remaining Monday–Sunday scoring week.
- Ranks available players by estimated remaining-week fantasy points.
- Compares waiver options against lower-value players on your roster.
- Includes position, NBA team, ESPN injury designation, fantasy points per game, and remaining games.
- Displays no more than **5 add/drop comparisons**, matching the league's weekly move limit.
- On **Saturday and Sunday**, ranks up to 3 next-week pickups by their marginal effect on your actual starting lineup. It hydrates both free agents and your roster from player cards, including NBA schedules, season/recent splits, and ownership rates. It excludes free agents owned in **more than 50%** of leagues or with any reported FPPG rate above **30**. It protects rostered players above either threshold, and protects players with missing ownership data. It accounts for Eastern-time game dates, active lineup-slot limits, position eligibility, daily vs. scoring-period lineup locks, injury status/return dates, and ESPN news. It simulates each recommended move against the roster left by the prior hypothetical move. When no positive upgrade is found, it does not relax the filters just to fill three slots.
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

The script uses the unofficial [espn-api Python library](https://github.com/cwendt94/espn-api) to read league, roster, free-agent, schedule, projection, recent-average, injury, and player-news data.

### Weekly waiver model

The ESPN library's `League.free_agents()` method creates free-agent Player objects without the NBA pro schedule. The report rehydrates both free agents and roster players through ESPN player cards in batches, attaching the league's `pro_schedule`, season/recent stat splits, and raw player metadata. This also matters for roster valuation: incomplete roster objects previously produced 0.0 FPPG for Scottie Barnes and Fred VanVleet, creating fake +100-point swaps. The report now stops if active-roster scoring rates are missing. A schedule-coverage check prevents an empty schedule from being silently interpreted as zero games. ESPN ownership values are extracted directly from player-card JSON because the basketball `Player` wrapper does not expose those fields.

The library represents NBA game times as datetimes created from Unix timestamps. The report converts those values to **America/New_York** before deriving the calendar date, avoiding a UTC date rollover on GitHub's runners.

The weekend model evaluates the effect of each add/drop on the team's projected **active-lineup points**:

1. Read ESPN's active lineup slot counts and lineup lock mode from league settings.
2. Blend ESPN projected FPPG (40%), season average (30%), L30 (15%), L15 (10%), and L7 (5%), renormalizing when a split is absent. Blending the projection with actual production reduces one-week or early-season projection spikes.
3. Estimate expected games individually using the NBA schedule, injury designation, and expected return date.
4. Optimize the active lineup by position eligibility and slot limits. For daily game-time locks, optimize each day separately; for scoring-period locks, optimize one lineup across the week.
5. Apply hard waiver rules: exclude free agents above 50% ownership, above 30 FPPG in any projection/recent/season field, or with missing ownership data. Protect roster players above either numerical threshold or with missing ownership data. Test each remaining player against every unprotected roster player and greedily simulate up to three positive swaps, recalculating the lineup after each one.
6. If fewer than three positive moves exist, display the best remaining marginal options as watchlist alternatives with their estimated net impact. They are not mislabeled as upgrades.

Current limitations: ESPN projections and injury statuses can lag; expected-return dates are not guarantees; the model does not yet ingest a separate minutes/rotation projection, opponent defensive matchup ratings, Vegas totals, or external injury feeds. The waiver scan is limited to the top `500` free agents returned by ESPN. Ownership and scoring filters are deliberately strict, so fewer than three options may be shown. No player can be valued perfectly without a validated projection/backtest dataset. The report is intended as decision support, not an automatic transaction engine.

### Design references

These projects informed the design choices; their code and data pipelines are not bundled into this repository.

- [yaqzan/fantasy](https://github.com/yaqzan/fantasy): the closest architectural reference. Its weekly pickup engine models schedule availability, daily vs. weekly lineups, eligible roster slots, injuries, and incremental multi-claim what-if swaps.
- [sharadmv/nba-fantasy-optimize](https://github.com/sharadmv/nba-fantasy-optimize): models player outcomes from historical performance and evaluates lineup choices by matchup win probability rather than ranking players by raw average alone.
- [Hashtag Basketball](https://hashtagbasketball.com/user-guide): useful reference for league-specific rankings, per-game vs. total projections, recent-form blends, and schedule-grid-based streaming decisions.
- [Basketball Monster](https://basketballmonster.com/Weekly): useful reference for dynamic role/news-aware projections, advanced ownership/add-drop signals, schedule analysis, and weekly projections.

The long-term improvement path is to add a separate minutes/role projection source, track forecast errors over time, and calibrate the model against actual weekly fantasy totals from this league.
