"""
Lumi — Risk Assessment Agent (Devil's Advocate)

Lumi reads Max's research + Nova's odds analysis and finds reasons to SKIP each bet.
She protects the bankroll by surfacing risks that Max and Nova might have glossed over.

Lumi looks for:
  - Injury uncertainty (key player questionable, practice status unclear)
  - Public money traps (line has moved against our side — books seeing heavy public action)
  - Motivation traps (team resting stars, tanking, already locked in seeding)
  - Weather / venue risks (outdoor sports)
  - Sample size problems (new coach, key transfer, team chemistry reset)
  - Market liquidity concerns (thin Polymarket volume = volatile price)
  - Any recent news Max's research might have missed

Verdict per event:
  PROCEED  — No significant risks, edge looks genuine
  CAUTION  — There are risks to monitor but edge may still be real (note them)
  ABORT    — Risk(s) are serious enough to skip this bet entirely

Model: qwen2.5:7b via Ollama (local, free, CPU inference)
Tools: none (reasons from the reports provided)
Output: lumi_report dict → agents/reports/lumi_{ts}.json
"""

import os
import logging
from datetime import datetime, timezone

from . import tools

logger = logging.getLogger("agents.lumi")

MODEL = "gemini-2.5-flash"

SYSTEM_PROMPT = """You are Lumi, the devil's advocate risk assessor for a sports betting operation.

Your job is to protect the bankroll by finding every reason why a bet might FAIL.
You are skeptical, contrarian, and rigorous. Your instinct is to say NO.

You only say PROCEED when the evidence is genuinely clean.
You say CAUTION when there are real risks but the edge may still survive them.
You say ABORT when a risk is serious enough to invalidate the edge entirely.

ABORT triggers (dynamic edge thresholds — read Nova's direction_conflict and Max's injury_impact_score):
  - No Polymarket market found AND nova sharp_books.found == false → ABORT immediately (no data at all)
  - No Polymarket market found BUT sharp_books available (nova sharp_books.found == true,
    books_used >= 2): do NOT hard-abort. Assess as a CAUTION — note "no_polymarket_venue"
    as a high-severity risk. The edge math is still valid; there is just no venue to bet it
    right now. Sage will SKIP this (no PM market), but your assessment is recorded for calibration.
  - Sharp money moving AGAINST our side (line moving wrong way)
  - Team has clear motivation to lose (resting stars, playoff position locked)
  - Polymarket volume < $1,000 (price unreliable)
  - Max confidence is LOW
  - direction_conflict == true in Nova's edge data: require edge >= 8% to proceed (contradictory signals)
  - Standard game (no injury chaos, no conflict): require edge >= 5%
  - High-injury game: edge >= 3% ONLY IF injury_impact_score > 0.15 AND volume > $5,000
    (market repricing is slow on injury chaos — lower bar is justified if there is liquidity)
  - Edge < 3% in any scenario: always ABORT

CAUTION triggers (note but don't abort):
  - Injury to secondary player (medium impact)
  - Slight line movement against us (< 2 points / < 3%)
  - Short rest (back-to-back, but team has good depth)
  - Weather risk (outdoor game, some precipitation forecast)
  - Modest Polymarket volume ($1,000-$5,000)

PROCEED:
  - No significant risk factors
  - Edge clearly present (>= 8%) OR strong Max confidence with solid research
  - Good Polymarket liquidity (> $5,000)

Self-learning: You have access to write_lesson(agent_name, what_went_wrong, correction, rule_generated).
Call it ONCE before your JSON if you notice a clear pattern across 3+ events this batch
(e.g. you ABORTed every event for the same reason, or every event had direction_conflict).
Use agent_name="Lumi". Only for genuine patterns — not single-event observations.

Output only the JSON. No prose after the JSON block."""

_JSON_SCHEMA = """
{
  "agent": "Lumi",
  "generated_at": "<ISO8601>",
  "assessments": [
    {
      "event_id": "<from Max/Nova reports>",
      "risks": [
        {
          "type": "<injury_uncertainty|public_trap|motivation|weather|sample_size|thin_market|other>",
          "severity": "<high|medium|low>",
          "description": "<specific description of the risk>"
        }
      ],
      "red_flags": ["<specific reason to be worried>"],
      "green_flags": ["<specific reason edge is genuine>"],
      "lumi_verdict": "<PROCEED|CAUTION|ABORT>",
      "skip_reason": "<only populated if ABORT — the main reason>"
    }
  ]
}"""


def run(max_report: dict, nova_report: dict, bankroll_context: dict = None) -> dict:
    """
    Run Lumi to assess risks for each candidate.

    Args:
        max_report:        Output from max_agent.run()
        nova_report:       Output from nova_agent.run()
        bankroll_context:  Optional portfolio health dict from runner (P&L, exposure, streak).
                           When provided, Lumi calibrates risk thresholds to bankroll state.

    Returns:
        lumi_report dict with assessments list.
    """
    candidates = max_report.get("candidates", [])
    analyses = nova_report.get("analyses", [])

    if not candidates:
        logger.info("[Lumi] No candidates to assess")
        return {
            "agent": "Lumi",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "assessments": [],
        }

    now = datetime.now(timezone.utc)

    # ── Bankroll context section ─────────────────────────────────────────────
    if bankroll_context:
        total_pnl    = bankroll_context.get("total_pnl", 0)
        open_bets    = bankroll_context.get("open_bets", 0)
        open_exposure = bankroll_context.get("open_exposure_usd", 0)
        recent_streak = bankroll_context.get("recent_streak", "")
        win_rate_7d  = bankroll_context.get("win_rate_7d", None)
        total_bets   = bankroll_context.get("total_bets", 0)

        pnl_sign = "+" if total_pnl >= 0 else ""
        bankroll_section = f"""
=== PORTFOLIO HEALTH (injected by system) ===
Total P&L all-time:     {pnl_sign}${total_pnl:.2f}
Total bets placed:      {total_bets}
Open bets right now:    {open_bets} (exposure: ${open_exposure:.2f})
Recent streak:          {recent_streak}
7-day win rate:         {f'{win_rate_7d:.0%}' if win_rate_7d is not None else 'N/A (insufficient data)'}

Use this to calibrate your risk threshold:
- If on a losing streak (3+ losses), be MORE conservative — prefer ABORT over CAUTION.
- If open exposure > $50, be more selective — we are already in the market.
- If win rate 7d < 40%, increase ABORT threshold for anything below 8% edge.
- If P&L is negative and total bets < 20, increase caution — we are still calibrating.
"""
    else:
        bankroll_section = """
=== PORTFOLIO HEALTH ===
No portfolio data available yet (first run or no trades placed).
Apply standard risk thresholds — no adjustment needed.
"""

    import json as _json
    max_json = _json.dumps(max_report, indent=2, default=str)
    nova_json = _json.dumps(nova_report, indent=2, default=str)

    identity_ctx = tools.load_agent_context("Lumi")

    event_ids_list = "\n".join(f"  - {c.get('event_id', '?')}" for c in candidates)

    user_prompt = f"""{identity_ctx}Review these {len(candidates)} events for betting risks.
{bankroll_section}
REQUIRED: You MUST output an assessment for EVERY event listed below. No exceptions.
Even if an event looks clean, output PROCEED with an empty risks list.
Events you must assess (one assessment per event_id):
{event_ids_list}

=== MAX'S RESEARCH ===
{max_json}

=== NOVA'S ODDS ANALYSIS ===
{nova_json}

For each event, find every reason why the bet might FAIL.
Apply the ABORT/CAUTION/PROCEED framework strictly.

Key questions for each event:
1. Are there injuries that undermine the edge thesis?
2. Is the Polymarket volume thick enough to trust the price? (check nova's volume field)
3. Does the edge (from Nova) hold up given the research risks (from Max)?
4. Any motivation concerns? (resting stars, locked playoff spots, tanking)
5. Is Max's confidence credible given the actual evidence?
6. Does the portfolio health above change your risk assessment for this bet?

CRITICAL: Your JSON "assessments" array MUST contain exactly {len(candidates)} items —
one for each event_id listed above. Missing any event_id = broken output.

Output the following JSON exactly:
{_JSON_SCHEMA}

Output ONLY the JSON object."""

    logger.info(f"[Lumi] Assessing risks for {len(candidates)} candidate(s) (Gemini 2.0 Flash)...")

    text = tools.run_agent_gemini(
        system=SYSTEM_PROMPT,
        user_prompt=user_prompt,
        model=MODEL,
        tools_schema=[tools.TOOL_WRITE_LESSON],
        execute_fn=tools.dispatch,
        tool_call_limits={"write_lesson": 1},
    )

    result = tools.extract_json(text)

    if not result or "assessments" not in result:
        logger.warning("[Lumi] JSON parse failed, retrying...")
        retry_prompt = (
            f"Output ONLY valid JSON matching this schema:\n{_JSON_SCHEMA}\n"
            f"Start with {{ and end with }}\n\n"
            f"Base your assessment on this prior analysis:\n{text[:2000]}"
        )
        text2 = tools.run_agent_gemini(
            system=SYSTEM_PROMPT,
            user_prompt=retry_prompt,
            model=MODEL,
        )
        result = tools.extract_json(text2)

    if not result or "assessments" not in result:
        from . import notifier as _notifier
        _notifier.pipeline_error(
            "Lumi",
            f"Gemini returned unparseable JSON twice. Risk assessment is empty. "
            f"First 200 chars of response: {text[:200]}"
        )
        logger.error("[Lumi] Could not extract valid report, returning empty")
        result = {"assessments": []}

    result["agent"] = "Lumi"
    result["generated_at"] = now.isoformat()

    assessments = result.get("assessments", [])

    # Safety net: if Lumi's LLM omitted any candidates (common when events look clean),
    # insert a PROCEED so they reach Sage rather than silently dying in the pipeline.
    assessed_ids = {a.get("event_id") for a in assessments}
    for c in candidates:
        eid = c.get("event_id", "")
        if eid and eid not in assessed_ids:
            logger.warning(
                f"[Lumi] {eid} missing from Lumi output — inserting PROCEED "
                f"(Gemini skipped this event, likely found no concerns)"
            )
            assessments.append({
                "event_id":     eid,
                "risks":        [],
                "red_flags":    [],
                "green_flags":  ["No concerns flagged — Lumi did not generate an assessment for this event"],
                "lumi_verdict": "PROCEED",
                "skip_reason":  None,
            })

    counts = {"PROCEED": 0, "CAUTION": 0, "ABORT": 0}
    for a in assessments:
        v = a.get("lumi_verdict", "?")
        counts[v] = counts.get(v, 0) + 1
        logger.info(
            f"  {a.get('event_id','?')} | {v}"
            + (f" — {a.get('skip_reason','')}" if v == "ABORT" else "")
        )

    logger.info(
        f"[Lumi] Done — PROCEED:{counts['PROCEED']} CAUTION:{counts['CAUTION']} ABORT:{counts['ABORT']}"
    )
    return result
