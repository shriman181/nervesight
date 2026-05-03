"""
llm_layer.py
────────────
Calls the Anthropic API to generate prescriptive, plain-English insights
from the computed analytics modules.

Designed to be called once per dashboard load (or on-demand).
Keeps context tight — sends only the key numbers, not raw data.

Usage:
    from llm_layer import generate_insights
    insights = generate_insights(customer_metrics, sku_metrics, receivables_report)
"""

import os
import json
from typing import Optional

try:
    import anthropic
    ANTHROPIC_AVAILABLE = True
except ImportError:
    ANTHROPIC_AVAILABLE = False


# ─────────────────────────────────────────
# Prompt builder
# ─────────────────────────────────────────

def _build_context(customer_metrics, sku_metrics, receivables_report) -> str:
    """Summarise analytics output into a tight prompt context."""

    # Customer summary (top 5 by margin)
    cust_lines = []
    for m in customer_metrics[:5]:
        cust_lines.append(
            f"  - {m.name} ({m.tier}): ₹{m.revenue_12m:,.0f} revenue, "
            f"{m.estimated_margin_pct:.1f}% margin, DSO {m.dso_days:.0f}d"
        )
    cust_block = "\n".join(cust_lines) if cust_lines else "  No customer data."

    # SKU summary (top 5)
    sku_lines = []
    for m in sku_metrics[:5]:
        sku_lines.append(
            f"  - {m.name} ({m.quadrant}): {m.gross_margin_pct:.1f}% margin, "
            f"{m.units_sold_monthly_avg:.0f} units/mo, dead_stock={m.is_dead_stock}"
        )
    sku_block = "\n".join(sku_lines) if sku_lines else "  No SKU data."

    dead_count = sum(1 for m in sku_metrics if m.is_dead_stock)
    dead_value = sum(m.stock_value for m in sku_metrics if m.is_dead_stock)

    # Receivables summary
    r = receivables_report
    recv_block = f"""
  Total outstanding: ₹{r.total_outstanding:,.0f}
  Total overdue: ₹{r.total_overdue:,.0f}
  Aging: 0-30d ₹{r.bucket_0_30:,.0f} | 31-60d ₹{r.bucket_31_60:,.0f} | 61-90d ₹{r.bucket_61_90:,.0f} | 90+d ₹{r.bucket_90_plus:,.0f}
  Collections efficiency (90d): {r.collections_efficiency_pct:.1f}%
  Top overdue party: {r.party_overdue[0].party_name if r.party_overdue else 'None'} — ₹{r.party_overdue[0].total_outstanding:,.0f} ({r.party_overdue[0].risk_level} risk)
  Anomaly flags: {len(r.anomaly_flags)}"""

    return f"""
BUSINESS CONTEXT: Indian B2B electrical distributor. Currency is INR (₹). 
This is a morning business intelligence brief. Be direct, specific, and actionable.

CUSTOMER INTELLIGENCE (top 5 by margin):
{cust_block}

SKU HEALTH (top 5):
{sku_block}
Dead stock: {dead_count} SKUs, ₹{dead_value:,.0f} locked in inventory

RECEIVABLES:
{recv_block}

ANOMALY FLAGS:
{chr(10).join('  ' + f for f in r.anomaly_flags) if r.anomaly_flags else '  None detected.'}
"""


SYSTEM_PROMPT = """You are NerveSight, an AI business analyst for Indian SME distributors.
Your job: read the analytics data and give the business owner 3-5 specific, numbered actions they can take TODAY.

Rules:
- Be direct. No fluff, no caveats, no "it's important to consider..."
- Use Indian business context (UPI, GST, distributor credit cycles, etc.)
- Lead with the most financially impactful action
- Keep each action to 2-3 sentences max
- End with one "watch out" flag if anything is alarming
- Respond in plain English, no markdown headers, no bullet symbols (use numbers only)
"""


# ─────────────────────────────────────────
# Main function
# ─────────────────────────────────────────

def generate_insights(
    customer_metrics: list,
    sku_metrics: list,
    receivables_report,
    api_key: Optional[str] = None,
) -> dict:
    """
    Returns dict with keys:
      - 'actions': list of action strings
      - 'watch_out': critical flag string or None
      - 'raw': full response text
      - 'error': error string if API call failed
    """
    if not ANTHROPIC_AVAILABLE:
        return _fallback_insights(customer_metrics, sku_metrics, receivables_report)

    key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return _fallback_insights(customer_metrics, sku_metrics, receivables_report)

    context = _build_context(customer_metrics, sku_metrics, receivables_report)

    try:
        client = anthropic.Anthropic(api_key=key)
        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=600,
            system=SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": f"Generate today's business intelligence brief:\n{context}"
                }
            ],
        )
        raw = message.content[0].text
        return _parse_response(raw)

    except Exception as e:
        return {
            "actions": [],
            "watch_out": None,
            "raw": "",
            "error": str(e),
        }


# ─────────────────────────────────────────
# Response parser
# ─────────────────────────────────────────

def _parse_response(raw: str) -> dict:
    """Split numbered actions from the watch_out flag."""
    lines = [l.strip() for l in raw.strip().split("\n") if l.strip()]
    actions = []
    watch_out = None

    for line in lines:
        if line.lower().startswith("watch out") or line.lower().startswith("⚠"):
            watch_out = line
        elif line and line[0].isdigit() and line[1] in (".", ")"):
            actions.append(line)
        elif actions:
            # continuation of the last action
            actions[-1] += " " + line

    return {
        "actions": actions,
        "watch_out": watch_out,
        "raw": raw,
        "error": None,
    }


# ─────────────────────────────────────────
# Fallback (no API key)
# ─────────────────────────────────────────

def _fallback_insights(customer_metrics, sku_metrics, receivables_report) -> dict:
    """Rule-based insights when Claude API is not available."""
    r = receivables_report
    actions = []

    # Action 1: Worst overdue party
    if r.party_overdue:
        top = r.party_overdue[0]
        actions.append(
            f"1. Call {top.party_name} today — ₹{top.total_outstanding:,.0f} is overdue "
            f"({top.oldest_overdue_days} days). Risk level: {top.risk_level}."
        )

    # Action 2: Drain customer
    drain = next((m for m in customer_metrics if m.tier == "Drain"), None)
    if drain:
        actions.append(
            f"2. Review terms with {drain.name} — generating revenue but only "
            f"{drain.estimated_margin_pct:.1f}% gross margin. Consider price revision or exit."
        )

    # Action 3: Dead stock
    dead = [m for m in sku_metrics if m.is_dead_stock]
    if dead:
        dead_val = sum(m.stock_value for m in dead)
        actions.append(
            f"3. Liquidate {len(dead)} dead stock SKU(s) worth ₹{dead_val:,.0f} — "
            f"offer discount to clear before month-end."
        )

    # Action 4: Sleeper customer
    sleeper = next((m for m in customer_metrics if m.tier == "Sleeper"), None)
    if sleeper:
        actions.append(
            f"4. Re-engage {sleeper.name} — no order in {sleeper.days_since_last_order} days. "
            f"They were a ₹{sleeper.revenue_12m:,.0f}/yr account."
        )

    # Action 5: Collections efficiency
    if r.collections_efficiency_pct < 80:
        actions.append(
            f"5. Collections efficiency is {r.collections_efficiency_pct:.1f}% — "
            f"₹{r.total_overdue:,.0f} is past due. Send payment reminders to all parties today."
        )

    watch_out = None
    critical = [po for po in r.party_overdue if po.risk_level == "Critical"]
    if critical:
        watch_out = (
            f"⚠ WATCH OUT: {critical[0].party_name} has ₹{critical[0].total_outstanding:,.0f} "
            f"overdue with ZERO payment history. Consider stopping further credit."
        )

    return {
        "actions": actions[:5],
        "watch_out": watch_out,
        "raw": "\n".join(actions),
        "error": None,
    }


# ──────────────────────────────────────────
# Quick test
# ──────────────────────────────────────────
if __name__ == "__main__":
    import sys, os
    sys.path.insert(0, os.path.dirname(__file__))
    from tally_connector import TallyConnector
    from analytics.customer_intelligence import compute_customer_metrics
    from analytics.inventory import compute_sku_metrics
    from analytics.receivables import compute_receivables

    conn = TallyConnector(
        mode="static",
        xml_path=os.path.join(os.path.dirname(__file__), "..", "data", "sample_tally_export.xml"),
    )
    data = conn.fetch_all()

    cm = compute_customer_metrics(data)
    sm = compute_sku_metrics(data)
    rr = compute_receivables(data)

    insights = generate_insights(cm, sm, rr)
    print("\n── NerveSight Daily Brief ──\n")
    for action in insights["actions"]:
        print(action)
    if insights["watch_out"]:
        print(f"\n{insights['watch_out']}")
    if insights["error"]:
        print(f"\n[API Error]: {insights['error']}")
