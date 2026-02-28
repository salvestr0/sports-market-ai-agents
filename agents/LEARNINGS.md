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

6. **EPL via web search = skip. EPL on Polymarket = research.** The NO_MARKET issue was Max finding EPL games via web search that were NOT on Polymarket. EPL events that appear in the ACTIVE POLYMARKET SPORTS MARKETS list ARE valid research targets and MUST be researched — they have confirmed slugs and may have sharp odds. Only block EPL games discovered via web search speculation (not in the Polymarket list).
