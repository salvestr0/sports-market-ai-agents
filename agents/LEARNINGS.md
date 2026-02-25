# Agent LEARNINGS â€” Mistake Log & Correction Rules



*When an agent makes a mistake, it is logged here with the correction and the rule it generates.*

*Rules are active â€” they override default behaviour on the next cycle.*

*Updated by self_review.py and by Sage after each batch.*



---



## Active Rules (All Agents)



1. **Absence of data is not permission to guess.** If sharp odds are missing, verdict is UNKNOWN â€” not VALUE. If injury status is unclear, flag it â€” don't assume healthy.

2. **Thin markets invalidate edges.** A 12% edge on a $400 Polymarket market is not a 12% edge. Volume floor is $1,000 â€” below that, the price is noise.

3. **Do not confuse correlation with causation.** If a team wins 70% at home, that is a stat. It is not an edge unless Polymarket is pricing them below that rate.

4. **One data point is not a pattern.** Do not generate a rule from a single outcome. Minimum 3 observations before concluding something.



---



## Max's Rules



1. **Always call get_injury_report() before forming any opinion.** Not after. Not optionally. A missed injury report means a flawed edge thesis.

2. **Vague confidence is dishonest confidence.** "MEDIUM" should not be a safe middle ground. If the evidence is thin, say LOW. Save HIGH for genuinely clean setups.

3. **The Polymarket list is the primary seed, not a secondary check.** Research the top-volume games first â€” they exist, they have markets, they matter.



---



## Nova's Rules



1. **Name mismatches cause silent errors.** "Rockets" vs "Houston Rockets" vs "HOU" â€” always verify the match before reporting an edge.

2. **NO_MARKET is a valid and important verdict.** Do not try to force a match where none exists. A clean NO_MARKET is better than a fabricated edge.

3. **Books_used < 2 means low confidence in the consensus.** Flag it in the notes â€” Sage and Lumi need to know the sharp consensus is thin.



---



## Lumi's Rules



1. **Rubber-stamping PROCEED is a failure mode.** If every event in a batch gets PROCEED, review whether the skepticism filter is actually running.

2. **Bankroll context changes the threshold.** Losing streak = stricter. High open exposure = more selective. Read the numbers, adjust accordingly.

3. **CAUTION without specifics is useless.** If flagging CAUTION, name the exact risk â€” not "there may be injury concerns" but "James Harden listed questionable, impact high, practice status unclear."



---



## Sage's Rules



1. **Rationalisation is the enemy.** If building a case for why an imperfect pick should be approved, the answer is SKIP.

2. **The pipeline_quality report is not bureaucracy â€” it is the feedback loop.** Write specific skip reasons so the team can improve next cycle.

3. **Memory is a responsibility.** Every batch gets logged. No exceptions. That log is how the team learns.



---



## Mistake Log



*(Appended chronologically â€” most recent last)*



| Date | Agent | What went wrong | Correction | Rule generated |

|------|-------|-----------------|------------|----------------|

| 2026-02-25 | Max | The `get_nba_game_log` tool consistently returned a timeout error for all teams across multiple attempts, making it impossible to fetch recent NBA game logs directly. | When `get_nba_game_log` fails, immediately fall back to `web_search` for 'TeamName last 5 games NBA [Month Year]' to gather recent form data. | Always use web_search for NBA recent form if get_nba_game_log fails. |
| 2026-02-25 | Lumi | Multiple events in this batch were aborted because the calculated numerical edge was consistently below the 3% minimum threshold, frequently combined with Max's low confidence or uncertain verdict, even when Max identified potential qualitative advantages. This indicates a consistent finding that small edges are unreliable, particularly with unverified data. | Lumi must continue to strictly enforce the minimum edge threshold of 3% in all scenarios, and prioritize this quantitative signal over qualitative theses when confidence is low or data is unverified. | Always ABORT if Nova's calculated edge is below 3% in any scenario, especially when combined with Max's LOW/UNCERTAIN confidence or unverified high-impact injury information. |
| 2026-02-25 | Pipeline | Across this batch (8/8 events) and multiple prior batches, Polymarket prices are consistently aligned with sharp books to within noise margin (edge 0.1%–1.2%), producing zero VALUE verdicts from Nova. Every event in this batch was also blocked by Lumi ABORT, continuing a streak of full-batch shutouts driven by edge_too_low as the dominant underlying signal. | The team should investigate whether the Polymarket NBA market is simply too efficient right now, with sharp arbitrageurs keeping prices within 1-2% of consensus. Prioritize researching non-NBA or non-peak markets (MMA, soccer, NHL) where Polymarket pricing may be less sharp. Also consider whether the 5% edge threshold is filtering correctly or if there are structural market hours where edges open up (e.g., early lines). | Always prioritize researching non-NBA markets when NBA Polymarket edges have been consistently below 2% across 3+ consecutive batches — NBA Polymarket appears to be in an efficient pricing regime. |
| 2026-02-25 | Lumi | In this batch, Lumi issued ABORT on all 8 events, including NBA_CLE_MIL where Max had HIGH confidence and a clear catastrophic-injury thesis (Giannis out), yet the numerical edge was only 0.10%. This confirms that even strong qualitative theses from Max are being consistently negated by negligible numerical edges — Polymarket has already priced in the information Max is finding. | Lumi is correctly applying the minimum edge threshold. However, Lumi should explicitly note in skip_reason when the market has already priced in the known injury/news, to help Max understand why high-confidence qualitative research still fails quantitative gates. | Always explicitly state 'market has priced in this information' in skip_reason when Max confidence is HIGH but numerical edge is below 3% — this feedback helps Max calibrate research targets, not just thesis quality. |
| 2026-02-25 | Max | The get_nba_game_log tool consistently returned a timeout error for all pre-fetched teams, making it impossible to fetch recent NBA game logs directly. | When get_nba_game_log fails or returns an error, immediately fall back to web_search for "TeamName last 5 games NBA [Month Year]" to gather recent form data. | Always use web_search for NBA recent form if get_nba_game_log fails or errors. |
| 2026-02-25 | Nova | In this batch, 3 of 8 events (NBA_POR_CHI, NBA_HOU_ORL, NBA_MIA_PHI) returned UNKNOWN due to no sharp odds found, despite having active Polymarket markets with significant volume. This is the third+ batch where no_sharp_odds accounts for multiple skips. | Nova should flag when Polymarket volume is high but sharp odds are missing — this suggests the game date/team name lookup may be failing (e.g., next-day games not yet listed on sharp books). Consider trying alternate sport keys or a date-offset search when the primary lookup fails. | Always attempt an alternate team-name or date-offset lookup when sharp odds are missing but Polymarket volume exceeds $100k — a high-volume PM market with no sharp odds is likely a lookup failure, not a genuine absence. |
| 2026-02-25 | Max | The `get_nba_game_log` tool and subsequent `web_search` for NBA recent form both failed to return any data across all pre-fetched NBA games, leading to a complete lack of recent form information for all researched NBA matchups. | When `get_nba_game_log` fails, and `web_search` for "TeamName last 5 games NBA [Month Year]" also yields no results, Max must clearly state in the `edge_thesis` that recent form data is unavailable, and downgrade confidence to "medium" or "low" as appropriate based on other available data. | Always explicitly state "recent form data unavailable" in the edge_thesis and adjust confidence downward when both `get_nba_game_log` and `web_search` fail to retrieve NBA recent form. |

