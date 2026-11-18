# ESPN Fantasy Basketball Assistant

Daily, read-only waiver analysis for **Scottie on the Spectrum** (ESPN league ID \`1850516159\`, team ID \`12\`).

## What it does

- Runs daily at approximately **9:45 AM Eastern** using GitHub Actions. Changes to \`main.py\` also trigger an immediate report, which makes model updates testable against current ESPN league data.
- Evaluates the **remaining Monday–Sunday scoring window** on weekdays and adds a next-week outlook on Saturday/Sunday.
- Uses **actual current-season ESPN fantasy points per game**, never ESPN projected FPPG, as the waiver-value baseline.
- Measures season production and stability with game-log games played, median, last-five FPPG, 20th-percentile floor, coefficient of variation, and the share of games reaching at least 75% of season FPPG.
- Shows daily roster schedule gaps, with particular attention to the manager's high-demand days: Monday, Tuesday, Thursday, Saturday, and Sunday.
- Compares waiver additions with every data-valid original-roster drop candidate by re-optimizing the active lineup day by day. **A pickup is recommended only if it can occupy an active eligible slot in every remaining game it is expected to play**; the model will not recommend a pickup that must be benched on one of its game dates.
- Respects ESPN position eligibility, active lineup slot counts, Eastern-time game dates, daily vs. scoring-period lineup locks, injury status/return dates, and the remaining NBA schedule.
- Pickup ownership is informational only. There is **no arbitrary 30-FPPG pickup ceiling or 26-FPPG drop threshold**; the add/drop decision comes from actual-season production and marginal lineup gain. Players without usable actual-season scoring data are not recommended and are not treated as safe drop candidates.
- Scans ESPN's NBA injury report and recent NBA news for potential role changes. A small role adjustment is applied only when a teammate is confirmed OUT/INACTIVE **and** the candidate's last-five minutes are at least 15% and 3 minutes above their earlier minutes baseline. Other same-team/position matches remain watchlist signals, not automatic boosts.
- Displays up to **3 distinct add/drop comparisons**. Each subsequent recommendation is re-evaluated after the prior hypothetical transaction, and only players from the original roster can be suggested as drops.
- Publishes the report in the GitHub Actions run summary.

**It never executes roster transactions.** Check ESPN for late injury news, starting status, and the remaining acquisition count.

## Required GitHub Actions secrets

In **Settings → Secrets and variables → Actions → Secrets**, add:

- \`ESPN_S2\`
- \`ESPN_SWID\`

Do not put ESPN cookies in repository Variables, source code, workflow YAML, issues, or commit history. Rotate credentials that were previously exposed in chat.

## Run it manually

Open **Actions → Daily ESPN Fantasy Basketball Report → Run workflow**. The result appears in the run's summary. A push changing \`main.py\` also starts the report.

## Schedule and daylight saving time

GitHub Actions cron uses UTC and may start late. The workflow schedules both 13:45 and 14:45 UTC, then checks the New York local clock so only the 9:45 AM Eastern run proceeds. GitHub does not guarantee execution at the exact minute.

## Methodology and limitations

The report uses the unofficial [espn-api Python library](https://github.com/cwendt94/espn-api) to read league, roster, free-agent, schedule, game-log, injury, and player-news data. ESPN's league-specific \`applied_total\` values are the actual fantasy points scored in each game under this league's scoring settings.

### Actual-production and reliability model

1. **Retrieve actual data.** \`League.free_agents()\` creates free-agent Player objects without the NBA schedule, so the report rehydrates both free agents and roster players through ESPN player cards. It also uses \`League.player_info(playerId=...)\` to merge event-level game stats where available. Game records are matched to NBA schedule IDs and restricted to completed games on or before the report date. Zero-minute DNPs are excluded from the played-game sample. ESPN's current-season \`appliedAverage\` is the season FPPG baseline; game logs provide distribution and minutes metrics.
2. **Build a robust per-game value.** For players with at least five logged games, the unadjusted model value is:
   \[
   0.70(\text{season mean}) + 0.20(\text{game-log median}) + 0.10(\text{shrunk L5 mean})
   \]
   The L5 mean is shrunk toward the season mean with five prior pseudo-games:
   \[
   \text{shrunk L5} =
   \frac{\sum \text{last five game scores} + 5(\text{season mean})}
        {\text{number of logged L5 games} + 5}
   \]
   This deliberately reduces the influence of a very short hot or cold run. With 3–4 logged games, the model uses 80% season mean and 20% median; with fewer than 3 game logs, it stays at the actual season mean.
3. **Account for game-to-game reliability.** The coefficient of variation (CV = population standard deviation / mean) imposes a bounded volatility penalty of at most 8%; it begins only above 0.45. The consistency rate is the percentage of logged games scoring at least 75% of the season mean; it applies a small adjustment bounded between −3.5% and +2%. Median, 20th-percentile floor, CV, and consistency are also exposed in the report so a manager can distinguish a high average with a fragile floor from a steadier scorer. A missing game-log sample lowers confidence rather than being mistaken for low variance.
4. **Ignore projections.** ESPN projected FPPG, projected weekly totals, historical reputation, ownership percentage, and positional ranking do not drive the production score or drop eligibility. Projections may exist on the underlying ESPN player object, but the waiver ranking does not consult them. No fixed FPPG ceiling excludes a productive free agent and no fixed FPPG threshold protects a roster player from a legitimate lineup comparison.
5. **Estimate remaining availability.** The model intersects each player's remaining NBA schedule with ESPN injury designation and expected-return data. A player listed OUT is not counted as playable absent a return date or a later status update. Questionable/day-to-day statuses are handled as availability uncertainty, not guaranteed games.
6. **Measure actual lineup opportunity.** The report reads ESPN's active roster slots, lock mode, and each player's \`eligibleSlots\`. On each date, it builds an eligibility graph between available players and active slots, then uses an augmenting-path bipartite matching routine to assign players to legal positions while prioritizing higher actual-production rates. It prints current roster games, active slots filled, and open slots for each remaining game date. This directly identifies empty days such as Thursday or Sunday rather than treating every scheduled game as equally useful.
7. **Evaluate marginal add/drop value.** For every free-agent / original-roster-drop pair, the model simulates the replacement roster, recalculates the legal lineup on each remaining date, and computes the change in actual-production lineup points. A pickup is rejected if any expected-available game cannot fit into an active slot. It greedily selects up to three distinct positive moves, re-running the comparison after each hypothetical move so that multiple suggestions do not all claim the same empty slot. This is a sequential marginal optimizer, not an exhaustive search over every possible multi-add bundle. Hypothetical pickups are never eligible to become later drop candidates.
8. **Treat injury news conservatively.** ESPN's injury feed and current NBA news are matched to same-team, same-position-group free agents. A reported teammate absence alone does not create points. A +2% to +6% FPPG adjustment requires a confirmed OUT/INACTIVE teammate plus an observed last-five minutes increase of at least 15% and 3 minutes versus the candidate's pre-L5 baseline. Otherwise, the affected players appear only in the injury/minutes watchlist. ESPN news matching can still be imperfect; check source links and NBA team assignments before acting.
9. **Use schedule as an opportunity, not a proxy for talent.** Three-game players remain eligible when their actual FPPG and open-slot schedule make them better additions than a lower-producing four-game player. Four-game team counts are a tiebreaker; the actual daily lineup simulation decides whether the extra game can be used.

### References informing the model

- [espn-api discussion: accessing a player's game-by-game scoring data](https://github.com/cwendt94/espn-api/discussions/611) — documents \`League.player_info(playerId=...)\`, \`Player.stats\`, \`applied_total\`, and matching game stats to the player schedule.
- [espn-api Basketball Player class](https://github.com/cwendt94/espn-api/wiki/Player-Class-Basketball) — documents season averages, projected averages, player schedules, and per-game stats.
- [HoopSplit methodology](https://hoopsplit.com/) — useful reference for reporting median, percentile floor/ceiling, and consistency alongside average production.
- [Josh Lloyd's fantasy consistency analysis](https://www.youtube.com/results?search_query=Josh+Lloyd+fantasy+basketball+consistency+coefficient+of+variation) — motivates distinguishing average production from variability; this implementation uses CV as a transparent, bounded heuristic rather than importing a proprietary rating.
- [Hashtag Basketball user guide](https://hashtagbasketball.com/user-guide) and [Basketball Monster weekly planner](https://basketballmonster.com/Weekly) — useful references for schedule-grid streaming, league-specific points, and incremental add/drop decisions.
- [FantasyProjections NBA season rankings](https://fantasyprojections.com/nba/rankings/season) — an independent example of ranking by realized season FPPG rather than player reputation. The assistant retrieves scoring data from the user's ESPN league, not this site.

### Limitations and validation path

This is a transparent rules-based heuristic, not a trained prediction model. The CV penalty and consistency adjustment are intentionally small and should be calibrated against future waiver accuracy results. No matchup-defense rating, Vegas total, official NBA depth-chart succession model, or full minutes forecast is currently applied. The injury role adjustment depends on the signal being correctly associated with the NBA team and player; ambiguous news remains a watchlist item. ESPN injury statuses and game logs can lag. ESPN's standard player card exposes a static current lineup slot and per-player eligibility, not pre-filled future daily lineup assignments, so the report predicts the best legal lineup from schedule and roster data. For daily game-time locks, it assumes the manager can set the best legal lineup before each game. Scoring-period-lock handling uses one fixed assignment for the period. The scan is limited to the top \`500\` free agents returned by ESPN. The model uses a greedy sequential search for up to three moves, so it does not guarantee the globally optimal three-claim package. It is decision support, not an automatic transaction engine.

The long-term improvement path is to backtest the risk/reliability weights against the Monday–Sunday actual points in this league, calibrate the role-change uplift, and add validated minutes/rotation inputs only when they measurably improve out-of-sample accuracy.
