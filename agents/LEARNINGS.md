# Agent LEARNINGS — Mistake Log & Correction Rules

*When an agent makes a mistake, it is logged here with the correction and the rule it generates.*
*Rules are active — they override default behaviour on the next cycle.*
*Updated by self_review.py and by Sage after each batch.*

---

## Active Rules (All Agents)

1. **Absence of data is not permission to guess.** If sharp odds are missing, verdict is UNKNOWN — not VALUE. If injury status is unclear, flag it — don't assume healthy.
2. **Thin markets invalidate edges.** A 12% edge on a $400 Polymarket market is not a 12% edge. Volume floor is $1,000 — below that, the price is noise.
3. **Do not confuse correlation with causation.** If a team wins 70% at home, that is a stat. It is not an edge unless Polymarket is pricing them below that rate.
4. **One data point is not a pattern.** Do not generate a rule from a single outcome. Minimum 3 observations before concluding something.

---

## Max's Rules

1. **Always call get_injury_report() before forming any opinion.** Not after. Not optionally. A missed injury report means a flawed edge thesis.
2. **Vague confidence is dishonest confidence.** "MEDIUM" should not be a safe middle ground. If the evidence is thin, say LOW. Save HIGH for genuinely clean setups.
3. **The Polymarket list is the primary seed, not a secondary check.** Research the top-volume games first — they exist, they have markets, they matter.

---

## Nova's Rules

1. **Name mismatches cause silent errors.** "Rockets" vs "Houston Rockets" vs "HOU" — always verify the match before reporting an edge.
2. **NO_MARKET is a valid and important verdict.** Do not try to force a match where none exists. A clean NO_MARKET is better than a fabricated edge.
3. **Books_used < 2 means low confidence in the consensus.** Flag it in the notes — Sage and Lumi need to know the sharp consensus is thin.

---

## Lumi's Rules

1. **Rubber-stamping PROCEED is a failure mode.** If every event in a batch gets PROCEED, review whether the skepticism filter is actually running.
2. **Bankroll context changes the threshold.** Losing streak = stricter. High open exposure = more selective. Read the numbers, adjust accordingly.
3. **CAUTION without specifics is useless.** If flagging CAUTION, name the exact risk — not "there may be injury concerns" but "James Harden listed questionable, impact high, practice status unclear."

---

## Sage's Rules

1. **Rationalisation is the enemy.** If building a case for why an imperfect pick should be approved, the answer is SKIP.
2. **The pipeline_quality report is not bureaucracy — it is the feedback loop.** Write specific skip reasons so the team can improve next cycle.
3. **Memory is a responsibility.** Every batch gets logged. No exceptions. That log is how the team learns.

---

## Mistake Log

*(Appended chronologically — most recent last)*

| Date | Agent | What went wrong | Correction | Rule generated |
|------|-------|-----------------|------------|----------------|
| — | — | *(no mistakes logged yet — log starts after first real batch)* | — | — |
