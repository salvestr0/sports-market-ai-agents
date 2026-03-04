"""
Sage — Final Decision Agent

Sage reads all three reports and makes the final BET/SKIP decision per event.
Approved picks are written to scraper_data/ in sports_bot.py format for execution.

Decision rules:
  BET when ALL of:
    - Nova verdict == VALUE (edge >= 5%, or >= 3% when Max conviction aligned)
    - Lumi verdict != ABORT
    - Max confidence != low
    - Polymarket market found

  SKIP when ANY of:
    - Nova verdict is NO_MARKET, OVERPRICED, or FAIR
    - Lumi verdict is ABORT
    - Max confidence is low or UNCERTAIN

Model: claude-sonnet-4-6 (synthesis and judgment)
Tools: none
Output: writes scraper_data/agents_{ts}.json (sports_bot.py pick format)
"""

import os
import json
import logging
from datetime import datetime, timezone

from . import tools, batch_db

logger = logging.getLogger("agents.sage")

MODEL = "claude-sonnet-4-6"

SYSTEM_PROMPT = """You are Sage, the final portfolio manager for a sports value betting operation.

You synthesize Max's research, Nova's odds analysis, and Lumi's risk assessment into final BET/SKIP decisions.

Your decision rules are strict:
  BET requires ALL of the following:
    1. Nova verdict == VALUE
       - Standard threshold: 5% edge vs sharp books
       - Reduced threshold: 3% edge when Nova's edge.conviction_aligned == true
         (Max's directional lean independently confirmed Nova's best-edge side)
    2. Lumi verdict is PROCEED or CAUTION (not ABORT)
    3. Max verdict is not UNCERTAIN and Max confidence is not "low"
    4. Polymarket market was found (nova polymarket.found == true)

  SKIP if ANY of:
    - Nova: NO_MARKET, OVERPRICED, FAIR, or UNKNOWN
    - Lumi: ABORT
    - Max: UNCERTAIN or confidence == "low"
    - No Polymarket market found

  When Nova edge.conviction_aligned == true and edge_pct is 3-5%, apply extra scrutiny:
  check Lumi's assessment carefully and cap confidence at "medium" even if Max said "high".

  IMPORTANT: If an event has no Lumi assessment in the report, treat it as Lumi = PROCEED.
  Missing Lumi assessment is NOT a reason to skip — only an explicit ABORT blocks a bet.

  UNVERIFIED INJURY EXCEPTION — override UNCERTAIN block when ALL of:
    - Max verdict == UNCERTAIN
    - Max research.verified_injury_status shows "unverified" for home or away
    - Nova verdict == VALUE (genuine edge exists despite the uncertainty)
    - Lumi verdict is PROCEED or CAUTION
  In this case: approve the bet but cap confidence at "medium" regardless of Max's rating,
  and prepend "⚠️ Unverified injury:" to the pick notes. The edge is real — unverified ≠ no edge.

For BET decisions, populate the pick with:
  - model_probability: use Nova's consensus_sharp_prob for the selected side
  - confidence: use Max's confidence (but cap at "medium" if Lumi said CAUTION)
  - notes: synthesize Max's edge_thesis + Nova's edge_pct + Lumi's key risk (if any)
  - polymarket_slug: from Nova's polymarket.slug
  - market_type: "moneyline" or "totals" (from Max's candidate)
  - ou_line: populate for totals picks (from Max's candidate ou_line field); null for moneyline
  - selection: for totals picks, set to "Over" or "Under" (from Nova's edge.selection);
    for EPL Yes/No markets, set to "Yes" (from Nova's edge.selection — the outcome to buy)

Self-learning: After making decisions, check for patterns worth recording:
  - If you see a recurring skip reason across 3+ events (e.g. all lumi_abort, or all edge_low <3%),
    call write_lesson before returning your JSON output.
  - If this batch changes what the team should focus on, call update_brain.
  - Only call these tools for genuine patterns — not for single-event observations.

Output the picks JSON after any tool calls. No prose outside the JSON."""

_PICK_SCHEMA = """
{
  "generated_at": "<ISO8601>",
  "agent_pipeline": "Sage-Max-Nova-Lumi",
  "picks": [
    {
      "event_id": "<from Max>",
      "sport": "<odds_api_sport_key>",
      "league": "<NBA|NFL|etc>",
      "home_team": "<Full Name>",
      "away_team": "<Full Name>",
      "market_type": "<moneyline|totals>",
      "ou_line": null,
      "selection": "<team name | Over | Under | Yes>",
      "model_probability": 0.66,
      "confidence": "<high|medium|low>",
      "notes": "<Max thesis. Nova: X% edge vs Pinnacle. Lumi: [key risk if CAUTION]>",
      "event_start": "<ISO8601 UTC>",
      "polymarket_slug": "<slug from Nova>"
    }
  ]
}"""


def _compute_quality_report(candidates: list, nova_analyses: list, lumi_assessments: list, picks: list) -> dict:
    """
    Compute a structured pipeline quality report — deterministic, no LLM call.
    Tells the team exactly why each event was skipped and what it would take to bet it.
    """
    approved_ids = {p.get("event_id") for p in picks}
    nova_by_id   = {a.get("event_id"): a for a in nova_analyses}
    lumi_by_id   = {a.get("event_id"): a for a in lumi_assessments}

    skip_analysis = []
    for c in candidates:
        eid = c.get("event_id", "?")
        if eid in approved_ids:
            continue

        nova  = nova_by_id.get(eid, {})
        lumi  = lumi_by_id.get(eid, {})
        blockers = []

        nova_verdict = nova.get("nova_verdict", "UNKNOWN")
        if nova_verdict == "NO_MARKET":
            blockers.append("no_polymarket_market")
        elif nova_verdict == "UNKNOWN":
            blockers.append("no_sharp_odds_data")
        elif nova_verdict in ("FAIR", "OVERPRICED"):
            edge_pct = (nova.get("edge") or {}).get("edge_pct", 0)
            blockers.append(f"edge_too_low ({edge_pct:.1f}%, need >=5%)")

        lumi_verdict = lumi.get("lumi_verdict", "")
        if lumi_verdict == "ABORT":
            reason = lumi.get("skip_reason") or "risk too high"
            blockers.append(f"lumi_abort: {reason}")

        if c.get("max_verdict") == "UNCERTAIN":
            # Check if uncertainty is due to unverified injury (softer blocker) or genuine coin-flip
            vis = (c.get("research") or {}).get("verified_injury_status", {})
            if vis.get("home") == "unverified" or vis.get("away") == "unverified":
                blockers.append("max_uncertain_injury_unverified")
            else:
                blockers.append("max_verdict_uncertain")
        if c.get("confidence") == "low":
            blockers.append("max_confidence_low")

        skip_analysis.append({
            "event_id":    eid,
            "home_team":   c.get("home_team", "?"),
            "away_team":   c.get("away_team", "?"),
            "nova_verdict": nova_verdict,
            "lumi_verdict": lumi_verdict,
            "blockers":    blockers,
        })

    return {
        "events_researched":   len(candidates),
        "events_approved":     len(picks),
        "events_skipped":      len(candidates) - len(picks),
        "value_found_count":   sum(1 for a in nova_analyses if a.get("nova_verdict") == "VALUE"),
        "no_market_count":     sum(1 for a in nova_analyses if a.get("nova_verdict") == "NO_MARKET"),
        "no_sharp_odds_count": sum(1 for a in nova_analyses if a.get("nova_verdict") == "UNKNOWN"),
        "lumi_abort_count":    sum(1 for a in lumi_assessments if a.get("lumi_verdict") == "ABORT"),
        "skip_analysis":       skip_analysis,
    }


def run(
    max_report: dict,
    nova_report: dict,
    lumi_report: dict,
    scraper_folder: str = "scraper_data",
    bankroll_context: dict = None,
    funnel_stats: dict = None,
) -> dict:
    """
    Run Sage to make final decisions and write approved picks.

    Args:
        max_report:       Output from max_agent.run()
        nova_report:      Output from nova_agent.run()
        lumi_report:      Output from lumi_agent.run()
        scraper_folder:   Directory to write pick files (sports_bot.py reads this)
        bankroll_context: Optional portfolio health dict from runner.
        funnel_stats:     Optional pipeline funnel counts (pm_events_available, etc.)

    Returns:
        sage_report dict (also written to scraper_folder/agents_{ts}.json)
    """
    try:
        import anthropic
    except ImportError:
        raise ImportError("pip install anthropic")

    candidates = max_report.get("candidates", [])
    if not candidates:
        logger.info("[Sage] No candidates — nothing to decide")
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "agent_pipeline": "Sage-Max-Nova-Lumi",
            "picks": [],
        }

    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    now = datetime.now(timezone.utc)

    max_json = json.dumps(max_report, indent=2, default=str)
    nova_json = json.dumps(nova_report, indent=2, default=str)
    lumi_json = json.dumps(lumi_report, indent=2, default=str)

    identity_ctx = tools.load_agent_context("Sage")

    # ── Bankroll health section ──────────────────────────────────────────────
    if bankroll_context:
        total_pnl       = bankroll_context.get("total_pnl", 0)
        starting        = bankroll_context.get("starting_bankroll", 100)
        current_balance = bankroll_context.get("current_bankroll_estimate", starting)
        open_exposure   = bankroll_context.get("open_exposure_usd", 0)
        recent_streak   = bankroll_context.get("recent_streak", "")
        win_rate_7d     = bankroll_context.get("win_rate_7d", None)
        pnl_sign        = "+" if total_pnl >= 0 else ""
        wr_str          = f"{win_rate_7d:.0%}" if win_rate_7d is not None else "N/A"
        open_bets   = bankroll_context.get("open_bets", 0)
        total_bets  = bankroll_context.get("total_bets", 0)
        bankroll_section = f"""
=== PORTFOLIO HEALTH ===
Starting bankroll:        ${starting:.2f}
Total P&L all-time:       {pnl_sign}${total_pnl:.2f}
Current estimated balance: ${current_balance:.2f}
Open bets (paper/live):   {open_bets} bet(s) currently open
Open exposure:            ${open_exposure:.2f}
Resolved bets (won/lost): {total_bets}
Recent streak:            {recent_streak}
7-day win rate:           {wr_str}

Sizing rule: never risk more than 5% of current balance on one bet.
If balance < $50 or on 3+ loss streak: BET only on picks with edge >= 8% AND Max confidence HIGH.
"""
    else:
        bankroll_section = """
=== PORTFOLIO HEALTH ===
No portfolio data available yet (first run or no trades placed).
Apply standard sizing rules — never risk more than 5% of starting bankroll on one bet.
"""

    # ── Batch history — gives Sage visibility into past performance ──────────
    history_summary = batch_db.get_summary(n_events=60)
    history_section = f"\n{history_summary}\n" if history_summary else ""

    if funnel_stats:
        funnel_section = (
            "\n=== PIPELINE FUNNEL (this batch) ===\n"
            f"Polymarket events available:  {funnel_stats['pm_events_available']}\n"
            f"Max researched:               {funnel_stats['max_researched']}  (top by volume)\n"
            f"Nova VALUE:                   {funnel_stats['nova_value']}\n"
            f"Nova NO_MARKET (PM missing):  {funnel_stats['nova_no_market']}\n"
            f"Nova UNKNOWN (no sharp odds): {funnel_stats['nova_unknown']}\n"
            f"Nova FAIR/OVERPRICED:         {funnel_stats['nova_fair_or_over']}\n"
        )
    else:
        funnel_section = ""

    user_prompt = f"""{identity_ctx}Make final BET/SKIP decisions for {len(candidates)} events.
{bankroll_section}{history_section}{funnel_section}
=== MAX'S RESEARCH ===
{max_json}

=== NOVA'S ODDS ANALYSIS ===
{nova_json}

=== LUMI'S RISK ASSESSMENT ===
{lumi_json}

Apply the decision rules strictly:
  BET: Nova==VALUE AND Lumi!=ABORT AND Max confidence!=low AND Polymarket found
  SKIP: anything else
  NOTE: If an event has no Lumi assessment, treat it as Lumi=PROCEED (not a blocker).

For each BET, populate the pick JSON. For SKIPs, do NOT include them in the output.
The "picks" array should contain ONLY approved bets.

If 0 events qualify, output: {{"generated_at": "...", "agent_pipeline": "Sage-Max-Nova-Lumi", "picks": []}}

Output the following JSON schema exactly:
{_PICK_SCHEMA}

Output ONLY the JSON. No other text."""

    logger.info(f"[Sage] Making final decisions on {len(candidates)} candidate(s)...")

    text = tools.run_agent(
        client=client,
        model=MODEL,
        system=SYSTEM_PROMPT,
        user_prompt=user_prompt,
        tools_schema=[tools.TOOL_WRITE_LESSON, tools.TOOL_UPDATE_BRAIN],
        execute_fn=tools.dispatch,
    )

    result = tools.extract_json(text)

    if not result or "picks" not in result:
        logger.warning("[Sage] JSON parse failed, retrying...")
        retry_prompt = (
            user_prompt
            + f"\n\nOutput ONLY valid JSON matching this schema:\n{_PICK_SCHEMA}\n"
            f"Start with {{ and end with }}"
        )
        text2 = tools.run_agent(
            client=client,
            model=MODEL,
            system=SYSTEM_PROMPT,
            user_prompt=retry_prompt,
            tools_schema=[tools.TOOL_WRITE_LESSON, tools.TOOL_UPDATE_BRAIN],
            execute_fn=tools.dispatch,
        )
        result = tools.extract_json(text2)

    if not result or "picks" not in result:
        from . import notifier as _notifier
        _notifier.pipeline_error(
            "Sage",
            f"Claude returned unparseable JSON twice. Picks output is empty. "
            f"First 200 chars of response: {text[:200]}"
        )
        logger.error("[Sage] Could not extract valid picks, writing empty output")
        result = {"picks": []}

    result["generated_at"] = now.isoformat()
    result["agent_pipeline"] = "Sage-Max-Nova-Lumi"

    picks = result.get("picks", [])
    logger.info(f"[Sage] {len(picks)} pick(s) approved from {len(candidates)} candidate(s)")

    # ── Pipeline quality report ────────────────────────────────────────────────
    quality = _compute_quality_report(
        candidates,
        nova_report.get("analyses", []),
        lumi_report.get("assessments", []),
        picks,
    )
    result["pipeline_quality"] = quality

    if quality["events_skipped"] > 0:
        logger.info("[Sage] Skip breakdown:")
        for skip in quality["skip_analysis"]:
            logger.info(
                f"  SKIP | {skip['home_team']} vs {skip['away_team']} — {', '.join(skip['blockers']) or 'unknown'}"
            )

    # Write to scraper_data/ for sports_bot.py to pick up
    os.makedirs(scraper_folder, exist_ok=True)
    ts = now.strftime("%Y%m%d_%H%M%S")
    output_path = os.path.join(scraper_folder, f"agents_{ts}.json")
    with open(output_path, "w") as f:
        json.dump(result, f, indent=2, default=str)
    logger.info(f"[Sage] Wrote picks → {output_path}")

    # ── Direct execution ──────────────────────────────────────────────────────
    execution_results = []
    if picks:
        logger.info(f"[Sage] Executing {len(picks)} pick(s) via executor...")
        try:
            from . import executor as _executor
            execution_results = _executor.execute_picks(picks)
        except Exception as e:
            logger.error(f"[Sage] Executor error: {e}", exc_info=True)
            from . import notifier as _notifier
            _notifier.pipeline_error("Executor", str(e))

        result["execution_results"] = [
            {
                "selection": r["pick"].get("selection", "?"),
                "status":    r["status"],
                "reason":    r["reason"],
                "price":     r["price"],
                "size_usd":  r["size_usd"],
                "order_id":  r["order_id"],
                "db_id":     r["db_id"],
            }
            for r in execution_results
        ]

    for pick in picks:
        exec_r = next(
            (r for r in execution_results
             if r["pick"].get("selection") == pick.get("selection")),
            None,
        )
        status_str = f" → {exec_r['status'].upper()}" if exec_r else ""
        logger.info(
            f"  BET | {pick.get('league','?')} | {pick.get('selection','?')} "
            f"@ model_prob={pick.get('model_probability','?')} | "
            f"conf={pick.get('confidence','?')}{status_str}"
        )

    if execution_results:
        from . import notifier as _notifier
        _notifier.bets_placed(execution_results)

    batch_id = now.strftime("%Y%m%d_%H%M%S")
    _write_batch_memory(now, candidates, quality, picks)
    _update_brain_md(now, candidates, nova_report.get("analyses", []), lumi_report.get("assessments", []), picks, quality)
    batch_db.log_batch(
        batch_id=batch_id,
        batch_ts=now,
        candidates=candidates,
        nova_analyses=nova_report.get("analyses", []),
        lumi_assessments=lumi_report.get("assessments", []),
        picks=picks,
    )

    return result


def _write_batch_memory(now, candidates, quality, picks):
    """
    Append a short one-liner to agents/memory.md.
    Full per-event detail lives in batch_history.db — this file stays concise
    so it remains readable when injected into agent prompts.
    """
    from pathlib import Path
    memory_path = Path("agents/memory.md")
    if not memory_path.exists():
        return

    ts = now.strftime("%Y-%m-%d %H:%M UTC")

    approved_str = (
        ", ".join(p.get("selection", "?") for p in picks)
        if picks else "none"
    )

    # Count each short blocker code (no lumi prose — detail is in batch_history.db)
    blocker_counts: dict = {}
    for skip in quality.get("skip_analysis", []):
        for b in skip.get("blockers", []):
            # Condense e.g. "edge_too_low (3.1%, need >=5%)" → "edge_low"
            if b.startswith("edge_too_low"):
                key = "edge_low"
            elif b.startswith("lumi_abort"):
                key = "lumi_abort"
            elif b == "max_verdict_uncertain":
                key = "uncertain"
            elif b == "max_confidence_low":
                key = "max_low"
            elif b == "no_polymarket_market":
                key = "no_market"
            elif b == "no_sharp_odds_data":
                key = "no_odds"
            else:
                key = b
            blocker_counts[key] = blocker_counts.get(key, 0) + 1

    blocker_str = ", ".join(
        f"{k}×{v}" for k, v in sorted(blocker_counts.items(), key=lambda x: -x[1])
    ) if blocker_counts else "n/a"

    line = (
        f"- {ts} | {quality['events_researched']} researched, "
        f"{quality['events_approved']} approved | "
        f"picks: {approved_str} | blocks: {blocker_str}\n"
    )

    text = memory_path.read_text(encoding="utf-8")
    marker = "## Batch Log\n"
    idx = text.find(marker)
    if idx >= 0:
        insert_at = idx + len(marker) + 1  # +1 for blank line after header
        text = text[:insert_at] + line + text[insert_at:]
        memory_path.write_text(text, encoding="utf-8")
        logger.info("[Sage] Batch log written to agents/memory.md")


def _update_brain_md(now, candidates, nova_analyses, lumi_assessments, picks, quality):
    """
    Overwrite BRAIN.md's 'Last Batch Notes' section with current batch stats.
    Also refreshes the 'Currently Watching' metrics table.
    Called every batch so agents always see fresh working memory.
    """
    from pathlib import Path
    brain_path = Path("agents/BRAIN.md")
    if not brain_path.exists():
        return

    ts = now.strftime("%Y-%m-%d %H:%M UTC")

    # Tally skip reasons
    blocker_counts: dict = {}
    for skip in quality.get("skip_analysis", []):
        for b in skip.get("blockers", []):
            if b.startswith("edge_too_low"):   key = "edge_low"
            elif b.startswith("lumi_abort"):   key = "lumi_abort"
            elif b == "no_polymarket_market":  key = "no_market"
            elif b == "no_sharp_odds_data":    key = "no_odds"
            elif b == "max_verdict_uncertain": key = "uncertain"
            elif b == "max_confidence_low":    key = "max_low"
            else:                              key = b
            blocker_counts[key] = blocker_counts.get(key, 0) + 1

    top_skip = (
        max(blocker_counts, key=blocker_counts.get) if blocker_counts else "n/a"
    )
    blocker_str = ", ".join(
        f"{k}×{v}" for k, v in sorted(blocker_counts.items(), key=lambda x: -x[1])
    ) if blocker_counts else "none"

    value_count    = sum(1 for a in nova_analyses    if a.get("nova_verdict") == "VALUE")
    no_market_count= sum(1 for a in nova_analyses    if a.get("nova_verdict") == "NO_MARKET")
    abort_count    = sum(1 for a in lumi_assessments if a.get("lumi_verdict") == "ABORT")

    approved_str = (
        ", ".join(p.get("selection", "?") for p in picks) if picks else "none"
    )

    notes_block = (
        f"*(Overwritten each batch — ephemeral context only)*\n\n"
        f"**{ts}**\n"
        f"- Researched: {len(candidates)} | Approved: {len(picks)} | Skipped: {quality.get('events_skipped', 0)}\n"
        f"- Nova VALUE: {value_count} | NO_MARKET: {no_market_count}\n"
        f"- Lumi ABORT: {abort_count}\n"
        f"- Skip reasons: {blocker_str}\n"
        f"- Top blocker: {top_skip}\n"
        f"- Approved picks: {approved_str}\n"
    )

    text = brain_path.read_text(encoding="utf-8")

    # Replace "Last Batch Notes" section content (everything up to next ## heading)
    marker = "## Last Batch Notes\n"
    start_idx = text.find(marker)
    if start_idx >= 0:
        content_start = start_idx + len(marker)
        end_idx = text.find("\n## ", content_start)
        if end_idx < 0:
            end_idx = len(text)
        text = text[:content_start] + "\n" + notes_block + "\n" + text[end_idx:]
        brain_path.write_text(text, encoding="utf-8")
        logger.info("[Sage] BRAIN.md Last Batch Notes updated")


def generate_discussion(
    max_report: dict,
    nova_report: dict,
    lumi_report: dict,
    sage_report: dict,
) -> str:
    """
    Generate a group discussion between all 4 agents about this batch.
    Each agent reflects on what they found, flags improvements, and discusses
    what to watch going forward. Sent to Telegram after each batch.

    Returns plain text formatted for Telegram HTML (uses <b> and <i> tags).
    """
    try:
        import anthropic
    except ImportError:
        return ""

    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    picks = sage_report.get("picks", [])
    candidates = max_report.get("candidates", [])
    analyses = nova_report.get("analyses", [])
    assessments = lumi_report.get("assessments", [])

    picks_summary = (
        f"{len(picks)} bet(s) approved: " + ", ".join(p.get("selection", "?") for p in picks)
        if picks else "No bets approved this run."
    )

    quality = sage_report.get("pipeline_quality", {})
    skip_details = json.dumps(quality.get("skip_analysis", []), indent=2)

    # Give agents access to their shared memory files for the discussion
    from pathlib import Path as _Path
    _memory_text = ""
    _brain_text  = ""
    try:
        _mp = _Path("agents/memory.md")
        if _mp.exists():
            _full = _mp.read_text(encoding="utf-8")
            # Include only the Batch Log section (recent history) — keep prompt lean
            _log_idx = _full.find("## Batch Log")
            _pat_idx = _full.find("## Patterns", _log_idx + 1) if _log_idx >= 0 else -1
            if _log_idx >= 0:
                _memory_text = _full[_log_idx: _pat_idx if _pat_idx > 0 else _log_idx + 2000]
        _bp = _Path("agents/BRAIN.md")
        if _bp.exists():
            _brain_text = _bp.read_text(encoding="utf-8")[:1500]
    except Exception:
        pass

    _memory_section = (
        f"\n=== SHARED MEMORY (agents can reference this) ===\n{_memory_text}\n{_brain_text}\n"
        if (_memory_text or _brain_text) else ""
    )

    prompt = f"""You are writing a group chat message between 4 AI sports betting agents after completing their hourly analysis batch. The owner reads this on Telegram to stay informed.
{_memory_section}

Batch summary:
- Max researched: {len(candidates)} events
- Nova found VALUE on: {sum(1 for a in analyses if a.get('nova_verdict') == 'VALUE')} event(s)
- Lumi aborted: {sum(1 for a in assessments if a.get('lumi_verdict') == 'ABORT')} event(s)
- Sage approved: {picks_summary}
- No-market events: {quality.get('no_market_count', 0)} | No sharp-odds events: {quality.get('no_sharp_odds_count', 0)}

Max's key research findings:
{json.dumps([{"event": c.get("home_team","?") + " vs " + c.get("away_team","?"), "thesis": c.get("research",{}).get("edge_thesis",""), "verdict": c.get("max_verdict","")} for c in candidates], indent=2)}

Nova's edges:
{json.dumps([{"event": a.get("event_id","?"), "verdict": a.get("nova_verdict",""), "edge_pct": (a.get("edge") or {}).get("edge_pct",0)} for a in analyses], indent=2)}

Lumi's risks:
{json.dumps([{"event": a.get("event_id","?"), "verdict": a.get("lumi_verdict",""), "top_risk": (a.get("risks") or [{}])[0].get("description","") if a.get("risks") else ""} for a in assessments], indent=2)}

Skip reasons (from pipeline quality report):
{skip_details}

Write a natural group discussion between Max, Nova, Lumi, and Sage. Each agent should:
1. Comment specifically on what they found this batch (be specific, not generic)
2. If events were skipped, each agent says what THEY specifically would need to change to make those events bettable
3. Flag ONE actionable improvement for next run

Rules:
- Keep each agent to 2-3 sentences max
- Be analytical and specific — no filler phrases like "great work team"
- Reference the actual skip blockers (e.g. "edge was only 2.1%", "no Polymarket market found")
- Format using agent names in bold: <b>Max:</b>, <b>Nova:</b>, <b>Lumi:</b>, <b>Sage:</b>
- Use plain text otherwise (no markdown, just the HTML bold tags)
- Total length: 8-15 lines"""

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text if response.content else ""
        logger.info("[Sage] Discussion generated")
        return text
    except Exception as e:
        logger.warning(f"[Sage] Discussion generation failed: {e}")
        return ""
