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

5. **Confirmed injuries open a 2% edge exception.** When Max confidence is HIGH and at least one high-impact injury is verified ("confirmed_out" via web_search or grok_twitter), edge >= 2% is sufficient to proceed (volume > $5,000 required). Markets price in verified news gradually — 2% is genuine value in that scenario. The 3% floor applies when injuries are unverified or Max confidence is medium/low.

6. **EPL has no Polymarket coverage — do not research it from web search.** EPL events have produced NO_MARKET in every batch across pipeline history. Max must not pick up EPL games from web search speculation. Only research EPL if a confirmed slug appears in the ACTIVE POLYMARKET SPORTS MARKETS list.



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

4. **NBA/NHL efficiently priced does not mean never-edge.** These leagues are deprioritized in research because average edges are sub-2%. But a confirmed key injury creates genuine value even at 2–3%. Apply the confirmed-injury exception (Active Rule 5) when warranted — do not blanket-ABORT every NBA/NHL event.


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
| 2026-02-25 | Max | The `get_nba_game_log` tool consistently timed out, and subsequent `web_search` attempts for recent NBA and NHL game logs for both pre-fetched and non-pre-fetched teams also failed to return any results across the entire batch. This resulted in a complete absence of recent form data for all researched events. | When both `get_nba_game_log` and `web_search` fail to retrieve recent form data for a given sport/team, Max must clearly state "recent form data unavailable" in the `edge_thesis` and automatically adjust the confidence rating downwards from "high" to "medium" or "low" based on the overall data completeness, rather than attempting further futile searches. | Always explicitly state "recent form data unavailable" in the edge_thesis and adjust confidence downward when both get_nba_game_log and web_search fail to retrieve recent form. |
| 2026-02-25 | Lumi | In this batch, Lumi failed to generate substantive risk assessments for all 8 events, instead outputting boilerplate "No concerns flagged — Lumi did not generate an assessment for this event" for every event. This is a textbook rubber-stamp PROCEED failure mode — no actual risk analysis was performed despite multiple events having unverified high-impact injuries (SGA out, Giannis out, Tatum out), unknown recent form, and extreme injury_impact_scores (1.0 on multiple events). | Lumi must actively assess each event's specific risks: injury severity, verification status, form data gaps, market volume, and edge magnitude. When injury_impact_score >= 0.8 or key injuries are unverified, Lumi must produce specific named risks — not silence. Silence is not PROCEED; it is a failure to run the filter. | Always produce explicit named risks for every event where injury_impact_score >= 0.5 or any injury is marked 'unverified' with 'high' impact — a blank green_flags list with boilerplate text is a pipeline failure, not a clean bill of health. |
| 2026-02-26 | Max | The `get_nba_game_log` tool consistently returned a timeout error for all pre-fetched NBA teams, and no recent game logs were available, leading to a complete absence of recent form data for all researched NBA matchups. | When `get_nba_game_log` fails, and `web_search` for "TeamName last 5 games NBA [Month Year]" also yields no results, Max must clearly state in the `edge_thesis` that recent form data is unavailable, and downgrade confidence to "medium" or "low" as appropriate based on other available data. | Always explicitly state "recent form data unavailable" in the edge_thesis and adjust confidence downward when both `get_nba_game_log` and `web_search` fail to retrieve NBA recent form. |
| 2026-02-26 | Nova | In this batch, nba_cha_ind_20260226 produced a 33.97% edge anomaly — the Polymarket slug was 'nba-cha-ind-2026-02-26-1h-moneyline' (1st Half market) while Nova pulled full-game sharp odds from Pinnacle/Betfair. This is at minimum the second occurrence of a non-standard slug suffix (1h-moneyline, 2h, etc.) creating a false edge by comparing mismatched market types. | Nova must detect non-standard slug suffixes (-1h-moneyline, -2h, -spread, -total, etc.) and either (a) pull sharp odds for the matching market type, or (b) flag the edge as SUSPECT_MARKET_MISMATCH rather than VALUE. A 30%+ edge is almost never real — treat edges above 15% as a mismatch signal requiring manual verification. | Always flag edges above 15% as SUSPECT_MARKET_MISMATCH if the Polymarket slug contains a non-standard suffix (-1h, -2h, -spread, -total, -moneyline qualifier) — the edge is likely a full-game vs. partial-game market comparison artifact, not a genuine pricing gap. |
| 2026-02-26 | Lumi | For the second consecutive batch, Lumi issued boilerplate 'No concerns flagged — Lumi did not generate an assessment for this event' PROCEED verdicts for 7 of 8 events — including events with injury_impact_score of 0.7-1.0 and multiple unverified high-impact injuries. This is a persistent rubber-stamping failure despite the rule already being logged in LEARNINGS. | Lumi must treat any event with injury_impact_score >= 0.5 or any unverified high-impact injury as requiring a substantive named assessment. A boilerplate green_flags entry is a pipeline failure signal — Sage should treat boilerplate PROCEED on high-injury events as CAUTION, not clean PROCEED. | Always treat Lumi boilerplate PROCEED ('No concerns flagged — Lumi did not generate an assessment') as CAUTION (not clean PROCEED) when injury_impact_score >= 0.5 or any injury is marked high-impact — the filter failed to run, not confirmed clean. |
| 2026-02-26 | Pipeline | Both events in this batch (NBA DAL/MIN and MLB CLE/COL) returned NO_MARKET from Nova — no Polymarket market found and no sharp odds data. This continues a pattern where researched events have no tradeable market, wasting the research budget. Notably, the DAL/MIN event is from 2024-05-28 (a historical playoff game), suggesting Max is occasionally pulling stale/historical events rather than current ones. | Max must verify event_start dates are current/future before researching. Events with event_start in the past (relative to batch generation time) should be discarded immediately. Nova's NO_MARKET on both events confirms no live market exists for these games. | Always discard any candidate event where event_start is in the past at batch generation time — historical games have no Polymarket or sharp odds and will always produce NO_MARKET, wasting the research budget. |
| 2026-02-27 | Max | The get_nba_game_log tool consistently returned a timeout error for all pre-fetched NBA teams, and subsequent web_search attempts for recent NBA game logs also failed to return any results across the entire batch. This resulted in a complete absence of recent form data for all researched NBA events. | When get_nba_game_log fails, and web_search for "TeamName last 5 games NBA [Month Year]" also yields no results, Max must clearly state in the edge_thesis that recent form data is unavailable, and downgrade confidence to "medium" or "low" as appropriate based on other available data. | Always explicitly state "recent form data unavailable" in the edge_thesis and adjust confidence downward when both get_nba_game_log and web_search fail to retrieve NBA recent form. |
| 2026-02-27 | Lumi | Multiple events in this batch, specifically NBA 1st Half Moneyline markets, generated extremely high edges (e.g., 19.14%, 20.99%) that were flagged as SUSPECT_MARKET_MISMATCH. This pattern of false edges arises from Nova comparing 1st Half Polymarket markets against full-game sharp odds, a critical pipeline mismatch previously identified. | Lumi must continue to rigorously identify and ABORT events where the Polymarket slug indicates a partial-game market (e.g., '-1h-moneyline') and the computed edge is unrealistically high (e.g., >15%), as this is a strong indicator of a data comparison error rather than a genuine pricing inefficiency. Nova needs a structural fix to match market types. | Always ABORT with 'suspect_market_mismatch' if the Polymarket slug indicates a partial-game market (e.g., contains '-1h', '-2h', '-spread', '-total' qualifier) and Nova's calculated edge is greater than 15%, as this strongly suggests a mismatch between Polymarket and sharp odds market types. |
| 2026-02-27 | Pipeline | In this batch, 4 of 7 researched NBA events had Polymarket slugs with '-1h-moneyline' suffix (partial-game markets). This is at minimum the third consecutive batch where 1st-half moneyline markets are being seeded into the pipeline, generating false VALUE signals (19.14%, 20.99%) when compared against full-game sharp odds. These events consumed Max research budget and Nova analysis capacity without producing any bettable output. | Max must screen Polymarket candidate slugs before researching — any slug containing '-1h', '-2h', '-1h-moneyline', '-2h-moneyline', '-spread', or '-total' should be deprioritized or discarded in favor of full-game moneyline markets. Nova must also hard-reject VALUE verdicts on partial-game slugs until market-type matching is implemented. | Always discard Polymarket candidate events whose slug contains a partial-game suffix (-1h, -2h, -1h-moneyline, -2h-moneyline, -spread, -total) before Max research begins — these markets cannot be compared against full-game sharp odds and will always generate false VALUE signals, wasting the entire research budget on unbettable events. |
| 2026-02-27 | Pipeline | All 5 NHL events with Polymarket markets in this batch produced edges between 0.14% and 0.92% — all well below the 5% VALUE threshold, and all below even the 3% minimum. This continues the pattern of tight market pricing seen across NBA and now NHL markets, with zero VALUE signals generated from either sport across multiple consecutive batches. | NHL Polymarket markets appear to be as efficiently priced as NBA markets. The pipeline is consistently spending research budget on NHL events that produce sub-1% edges. Max should deprioritize NHL events unless there is a specific known catalyst (major goalie injury, back-to-back travel disadvantage, etc.) that Polymarket is unlikely to have priced in yet. | Always deprioritize NHL and NBA Polymarket events when the last 3+ batches have produced edges below 2% for those leagues — both markets appear efficiently priced; shift research budget toward MMA, lower-profile soccer, or markets with known structural pricing lags. |
| 2026-02-27 | Max | Multiple web_search calls across various categories (injuries, recent form, H2H, lineups) consistently returned connection timeout errors, leading to a complete absence of critical pre-game intelligence for the Wolverhampton Wanderers vs Aston Villa match. | When initial web_search attempts for multiple categories fail due to connection timeouts, retry a subset of the most critical queries (e.g., injuries, recent form, lineups for imminent games) before moving on. If retries also fail, clearly state 'data unavailable' in the edge_thesis and adjust confidence. | If multiple web_search calls fail due to connection timeouts for a given event, retry the most critical queries once. If failures persist, explicitly state 'data unavailable' and adjust confidence accordingly. |
| 2026-02-27 | Pipeline | For the third consecutive batch (2026-02-27 08:13, 13:29, 18:30), persistent web_search and API connection timeouts have caused Max to produce UNCERTAIN/low-confidence verdicts across all researched events. This is a systemic infrastructure failure that is nullifying the entire research pipeline — 0 of 6 events produced any usable intelligence, and 0 of 18+ events across three batches passed Max's filter. | When web_search timeouts are detected on the first 2 tool calls within a batch, Max should immediately abort the remaining research queue and flag a PIPELINE_DEGRADED status rather than continuing to research events with zero data. Sage should treat a full batch of UNCERTAIN/low verdicts as a systemic signal, not individual event failures. | Always halt the research batch and emit PIPELINE_DEGRADED if 3+ consecutive web_search or API calls fail with connection timeouts — continuing to research under total data blackout wastes compute and produces only UNCERTAIN/low verdicts that will all be skipped. |
| 2026-02-28 | Lumi | Max consistently identifies high-impact injury situations with "medium" confidence, but the underlying injury statuses are frequently "unverified" by Grok/web_search. Nova then calculates a negligible edge (below 3%), indicating the market has either priced in the uncertainty or the injury impact is not as significant as Max believes, despite Max's initial assessment of disparity. This pattern leads to speculative bets being considered due to the low confidence threshold combined with unreliable injury data. | Lumi should treat a "medium" confidence Max verdict, combined with a high injury_impact_score (>= 0.8) and "unverified" injury statuses, as a more severe risk, especially when the calculated numerical edge is below 3%. The unverified nature of critical injury data combined with a negligible edge makes the bet too speculative. | When Max's confidence is "medium", injury_impact_score is >= 0.8, all high-impact injuries are "unverified", and Nova's calculated edge is below 3%, ABORT immediately. The unverified nature of critical injury data combined with a negligible edge makes the bet too speculative. |
| 2026-02-28 | Pipeline | All 4 EPL events in this batch (BUR/BRE, LEE/MAC, NEW/EVE, LIV/WES) returned NO_MARKET from Nova — EPL has had zero Polymarket coverage across every batch in the log. Research budget is being spent on EPL events that will never produce a bettable output. | Max should deprioritize EPL events entirely unless a specific Polymarket slug is confirmed to exist before research begins. Nova's NO_MARKET on EPL is now a structural certainty, not an anomaly. | Always discard EPL candidate events before Max research unless a confirmed Polymarket slug exists — EPL has produced NO_MARKET in every batch across the pipeline history and is unbettable in this system. |

