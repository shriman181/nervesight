"""
main.py — NerveSight Lite Dashboard
────────────────────────────────────
Run with: streamlit run app/main.py
"""

import streamlit as st
import pandas as pd
import os
import sys
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))

from tally_connector import TallyConnector
from analytics.customer_intelligence import compute_customer_metrics
from analytics.inventory import compute_sku_metrics
from analytics.receivables import compute_receivables, aging_buckets_dict
from analytics.cashflow import compute_cashflow
from llm_layer import generate_insights

st.set_page_config(
    page_title="NerveSight — Business Intelligence",
    page_icon="🧠",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown("""
<style>
    .action-box {
        background: #f0f4ff;
        border-left: 4px solid #0066cc;
        padding: 12px 16px;
        border-radius: 0 8px 8px 0;
        margin: 8px 0;
        font-size: 14px;
        line-height: 1.6;
        color: #1a1a2e !important;
    }
    .action-box * { color: #1a1a2e !important; }
    .watchout-box {
        background: #fff0f0;
        border-left: 4px solid #dc3545;
        padding: 12px 16px;
        border-radius: 0 8px 8px 0;
        margin: 8px 0;
        font-size: 14px;
        font-weight: 500;
        color: #5c0011 !important;
    }
    .watchout-box * { color: #5c0011 !important; }
    .flag-box {
        background: #fffbea;
        border-left: 4px solid #ffc107;
        padding: 10px 14px;
        border-radius: 0 8px 8px 0;
        margin: 6px 0;
        font-size: 13px;
        color: #3d2e00 !important;
    }
    .flag-box * { color: #3d2e00 !important; }
    .critical-flag {
        background: #fff0f0;
        border-left: 4px solid #dc3545;
        padding: 10px 14px;
        border-radius: 0 8px 8px 0;
        margin: 6px 0;
        font-size: 13px;
        color: #5c0011 !important;
    }
    .critical-flag * { color: #5c0011 !important; }
    .info-flag {
        background: #f0f8ff;
        border-left: 4px solid #0066cc;
        padding: 10px 14px;
        border-radius: 0 8px 8px 0;
        margin: 6px 0;
        font-size: 13px;
        color: #003366 !important;
    }
    .info-flag * { color: #003366 !important; }
    .insight-card {
        background: #f8f9fa;
        border-radius: 10px;
        padding: 14px 18px;
        margin: 6px 0;
        border: 1px solid #e0e0e0;
        color: #1a1a2e !important;
    }
    .insight-card * { color: #1a1a2e !important; }
    .section-label {
        font-size: 11px;
        text-transform: uppercase;
        letter-spacing: 0.08em;
        color: #888;
        margin-bottom: 4px;
    }
</style>
""", unsafe_allow_html=True)


@st.cache_data(ttl=300)
def load_all():
    xml_path = os.path.join(ROOT, "data", "sample_tally_export.xml")
    conn = TallyConnector(mode="static", xml_path=xml_path)
    data = conn.fetch_all()
    customers = compute_customer_metrics(data)
    skus = compute_sku_metrics(data)
    receivables = compute_receivables(data)
    cashflow = compute_cashflow(data)
    return customers, skus, receivables, cashflow


# ── Header ────────────────────────────────
c1, c2, c3 = st.columns([1, 7, 2])
with c1:
    st.markdown("## 🧠")
with c2:
    st.markdown("## NerveSight — Business Intelligence")
    st.caption(f"Powered by Tally data · Lite v0.1 · Demo mode · {date.today().strftime('%d %b %Y')}")
with c3:
    st.markdown("<br>", unsafe_allow_html=True)
    if st.button("🔄 Refresh Data"):
        st.cache_data.clear()
        st.rerun()

st.divider()

with st.spinner("Reading Tally data..."):
    try:
        customers, skus, receivables, cashflow = load_all()
    except Exception as e:
        st.error(f"Failed to load data: {e}")
        st.stop()

# ── Scorecard ─────────────────────────────
total_revenue = sum(m.revenue_12m for m in customers)
total_margin = sum(m.estimated_margin_abs for m in customers)
avg_margin_pct = (total_margin / total_revenue * 100) if total_revenue > 0 else 0
dead_count = sum(1 for m in skus if m.is_dead_stock)
dead_value = sum(m.stock_value for m in skus if m.is_dead_stock)
overdue_pct = (receivables.total_overdue / receivables.total_outstanding * 100) if receivables.total_outstanding > 0 else 0
star_count = sum(1 for m in customers if m.tier == "Star")
risky_count = sum(1 for m in customers if m.tier == "Risky")

st.subheader("📊 Morning Scorecard")
k1, k2, k3, k4, k5, k6, k7 = st.columns(7)
k1.metric("Revenue (12M)", f"₹{total_revenue/100000:.1f}L")
k2.metric("Gross Margin", f"{avg_margin_pct:.1f}%")
k3.metric("Total Overdue", f"₹{receivables.total_overdue/100000:.1f}L",
          delta=f"{overdue_pct:.0f}% of outstanding", delta_color="inverse")
k4.metric("Collections Eff.", f"{receivables.collections_efficiency_pct:.0f}%",
          delta="last 90 days", delta_color="off")
k5.metric("Dead Stock", f"{dead_count} SKUs",
          delta=f"₹{dead_value/1000:.0f}K locked", delta_color="inverse")
k6.metric("⭐ Stars", str(star_count))
k7.metric("🔴 Risky", str(risky_count),
          delta="needs action" if risky_count > 0 else "all clear",
          delta_color="inverse" if risky_count > 0 else "normal")

st.divider()

# ── AI Brief ──────────────────────────────
st.subheader("🤖 Today's Action Brief")
api_key = os.environ.get("ANTHROPIC_API_KEY", "")
with st.spinner("Generating insights..."):
    insights = generate_insights(customers, skus, receivables, api_key=api_key or None)

col_brief, col_health = st.columns([3, 1])
with col_brief:
    for action in insights["actions"]:
        st.markdown(f'<div class="action-box">{action}</div>', unsafe_allow_html=True)
    if insights.get("watch_out"):
        st.markdown(f'<div class="watchout-box">{insights["watch_out"]}</div>', unsafe_allow_html=True)

with col_health:
    total_anomalies = len(receivables.anomaly_flags)
    health_score = max(0, 100 - (risky_count * 15) - (dead_count * 5)
                       - (total_anomalies * 8) - max(0, 80 - receivables.collections_efficiency_pct))
    color = "#28a745" if health_score >= 70 else "#ffc107" if health_score >= 45 else "#dc3545"
    label = "Healthy" if health_score >= 70 else "Needs attention" if health_score >= 45 else "At risk"
    st.markdown(f"""
    <div style='text-align:center;padding:20px;border-radius:12px;border:2px solid {color};margin-bottom:12px;'>
        <div style='font-size:42px;font-weight:700;color:{color};'>{health_score}</div>
        <div style='font-size:13px;color:{color};font-weight:600;'>{label}</div>
        <div style='font-size:11px;color:#888;margin-top:4px;'>Business health score</div>
    </div>""", unsafe_allow_html=True)
    st.markdown(f"**{risky_count}** risky customers")
    st.markdown(f"**{dead_count}** dead stock SKUs")
    st.markdown(f"**{total_anomalies}** anomaly flags")
    st.markdown(f"**{receivables.collections_efficiency_pct:.0f}%** collections eff.")

st.divider()

# ══════════════════════════════════════════
# TABS
# ══════════════════════════════════════════
tab1, tab2, tab3, tab4, tab5 = st.tabs([
    "👥 Customers", "📦 Inventory", "💰 Receivables", "📈 Cash Flow", "🔍 Anomalies"
])


# ══════════════════════════════════════════
# TAB 1 — CUSTOMERS
# ══════════════════════════════════════════
with tab1:
    st.markdown("### Customer Profitability & Health")
    st.caption("Every customer ranked by gross margin. Revenue rank and margin rank are almost never the same list.")

    rows = []
    for m in customers:
        rows.append({
            "Customer": m.name,
            "City": m.city,
            "Revenue ₹": int(m.revenue_12m),
            "Margin %": round(m.estimated_margin_pct, 1),
            "Margin ₹": int(m.estimated_margin_abs),
            "Outstanding ₹": int(m.outstanding_balance),
            "DSO (days)": int(m.dso_days),
            "Orders (12M)": m.order_count_12m,
            "Avg Order ₹": int(m.avg_order_value),
            "Last Order": str(m.last_order_date) if m.last_order_date else "—",
            "Days Silent": m.days_since_last_order,
            "Ever Paid": "✓" if m.has_ever_paid else "✗",
            "Tier": m.tier,
            "Why": m.tier_reason,
        })
    df = pd.DataFrame(rows)

    all_tiers = ["All"] + sorted(df["Tier"].unique().tolist())
    sel_tier = st.selectbox("Filter by tier", all_tiers, key="tier_filter")
    df_f = df if sel_tier == "All" else df[df["Tier"] == sel_tier]

    st.dataframe(
        df_f.drop(columns=["Why"]),
        use_container_width=True, hide_index=True,
        column_config={
            "Revenue ₹": st.column_config.NumberColumn(format="₹%d"),
            "Margin ₹": st.column_config.NumberColumn(format="₹%d"),
            "Outstanding ₹": st.column_config.NumberColumn(format="₹%d"),
            "Avg Order ₹": st.column_config.NumberColumn(format="₹%d"),
            "Margin %": st.column_config.NumberColumn(format="%.1f%%"),
            "DSO (days)": st.column_config.NumberColumn(format="%d d"),
            "Days Silent": st.column_config.NumberColumn(format="%d d"),
        },
    )

    st.divider()
    ch1, ch2 = st.columns(2)

    with ch1:
        st.markdown("#### Revenue vs Margin")
        st.caption("The gap between these bars reveals who's destroying margin behind high revenue.")
        st.bar_chart(df.set_index("Customer")[["Revenue ₹", "Margin ₹"]])

    with ch2:
        st.markdown("#### Tier Distribution")
        st.bar_chart(df["Tier"].value_counts())

    st.divider()
    st.markdown("#### DSO by Customer")
    st.caption("Above 45 days in Indian B2B distribution = red flag. Above 90 days = stop credit.")
    st.bar_chart(df.sort_values("DSO (days)", ascending=False).set_index("Customer")["DSO (days)"])

    st.divider()
    st.markdown("#### Customer Tier Cards")
    tg1, tg2 = st.columns(2)

    stars_c = [m for m in customers if m.tier == "Star"]
    drains_c = [m for m in customers if m.tier == "Drain"]
    risky_c = [m for m in customers if m.tier == "Risky"]
    sleepers_c = [m for m in customers if m.tier == "Sleeper"]

    with tg1:
        if stars_c:
            st.markdown("**⭐ Stars — protect these**")
            for m in stars_c:
                st.markdown(f'<div class="insight-card"><b>{m.name}</b><br>'
                            f'₹{m.revenue_12m:,.0f} revenue · {m.estimated_margin_pct:.1f}% margin · DSO {m.dso_days:.0f}d<br>'
                            f'<small style="color:#666">{m.tier_reason}</small></div>', unsafe_allow_html=True)
        if drains_c:
            st.markdown("**🟡 Drains — revenue without profit**")
            for m in drains_c:
                st.markdown(f'<div class="flag-box"><b>{m.name}</b><br>'
                            f'₹{m.revenue_12m:,.0f} revenue · only {m.estimated_margin_pct:.1f}% margin<br>'
                            f'<small>{m.tier_reason}</small></div>', unsafe_allow_html=True)

    with tg2:
        if risky_c:
            st.markdown("**🔴 Risky — stop credit, escalate now**")
            for m in risky_c:
                st.markdown(f'<div class="critical-flag"><b>{m.name}</b><br>'
                            f'₹{m.outstanding_balance:,.0f} outstanding · Paid before: {"Yes" if m.has_ever_paid else "NEVER"}<br>'
                            f'<small>{m.tier_reason}</small></div>', unsafe_allow_html=True)
        if sleepers_c:
            st.markdown("**🔵 Sleepers — re-engage**")
            for m in sleepers_c:
                st.markdown(f'<div class="insight-card"><b>{m.name}</b><br>'
                            f'Silent for {m.days_since_last_order} days · Was ₹{m.revenue_12m:,.0f}/yr<br>'
                            f'<small style="color:#666">{m.tier_reason}</small></div>', unsafe_allow_html=True)


# ══════════════════════════════════════════
# TAB 2 — INVENTORY
# ══════════════════════════════════════════
with tab2:
    st.markdown("### SKU & Segment Health")
    st.caption("Every SKU ranked by margin × velocity. Dead weight surfaces at the bottom.")

    sku_rows = []
    for m in skus:
        sku_rows.append({
            "SKU": m.name,
            "Category": m.category,
            "Margin %": round(m.gross_margin_pct, 1),
            "Margin/unit ₹": round(m.gross_margin_abs_per_unit, 0),
            "Avg Sale ₹": round(m.avg_sale_rate, 0),
            "Avg Cost ₹": round(m.avg_purchase_rate, 0),
            "Units/mo": round(m.units_sold_monthly_avg, 1),
            "Stock QTY": int(m.stock_on_hand),
            "Stock Value ₹": int(m.stock_value),
            "Trend": m.velocity_trend,
            "Returns %": round(m.return_rate_pct, 1),
            "Last Sale": str(m.last_sale_date) if m.last_sale_date else "Never",
            "Quadrant": m.quadrant,
            "Dead?": "⚠ YES" if m.is_dead_stock else "",
        })
    df_sku = pd.DataFrame(sku_rows)

    all_q = ["All"] + sorted(df_sku["Quadrant"].unique().tolist())
    sel_q = st.selectbox("Filter by quadrant", all_q, key="quad_filter")
    df_sku_f = df_sku if sel_q == "All" else df_sku[df_sku["Quadrant"] == sel_q]

    st.dataframe(
        df_sku_f, use_container_width=True, hide_index=True,
        column_config={
            "Margin %": st.column_config.NumberColumn(format="%.1f%%"),
            "Returns %": st.column_config.NumberColumn(format="%.1f%%"),
            "Stock Value ₹": st.column_config.NumberColumn(format="₹%d"),
            "Avg Sale ₹": st.column_config.NumberColumn(format="₹%.0f"),
            "Avg Cost ₹": st.column_config.NumberColumn(format="₹%.0f"),
            "Margin/unit ₹": st.column_config.NumberColumn(format="₹%.0f"),
        },
    )

    st.divider()
    sc1, sc2 = st.columns(2)

    with sc1:
        st.markdown("#### Gross Margin % by SKU")
        st.caption("Below 15% = volume trap. Below 10% = question whether to stock it.")
        st.bar_chart(df_sku.set_index("SKU")["Margin %"].sort_values(ascending=True))

    with sc2:
        st.markdown("#### Stock Value by Category")
        st.caption("Where your working capital is parked.")
        cat_df = df_sku.groupby("Category")["Stock Value ₹"].sum().sort_values(ascending=False)
        st.bar_chart(cat_df)

    st.divider()
    sc3, sc4 = st.columns(2)

    with sc3:
        st.markdown("#### Monthly Sales Velocity")
        st.caption("Units sold per month (3-month avg). Flat or falling = investigate.")
        st.bar_chart(df_sku.set_index("SKU")["Units/mo"].sort_values(ascending=False))

    with sc4:
        st.markdown("#### Return Rate by SKU")
        st.caption("Above 5% = quality issue or wrong customer fit.")
        st.bar_chart(df_sku.set_index("SKU")["Returns %"].sort_values(ascending=False))

    st.divider()
    st.markdown("#### SKU Quadrant Breakdown")
    q1, q2, q3, q4 = st.columns(4)

    def sku_card(m):
        return (f'<div class="insight-card"><b>{m.name}</b><br>'
                f'{m.gross_margin_pct:.1f}% margin · {m.units_sold_monthly_avg:.0f} units/mo<br>'
                f'Stock: {m.stock_on_hand:.0f} units · ₹{m.stock_value:,.0f}</div>')

    with q1:
        st.markdown("**⭐ Stars**")
        stars_s = [m for m in skus if m.quadrant == "Star"]
        [st.markdown(sku_card(m), unsafe_allow_html=True) for m in stars_s] if stars_s else st.caption("None")

    with q2:
        st.markdown("**💰 Hidden Gems**")
        gems = [m for m in skus if m.quadrant == "Hidden Gem"]
        [st.markdown(sku_card(m), unsafe_allow_html=True) for m in gems] if gems else st.caption("None")

    with q3:
        st.markdown("**⚠ Volume Traps**")
        traps = [m for m in skus if m.quadrant == "Volume Trap"]
        if traps:
            for m in traps:
                st.markdown(f'<div class="flag-box"><b>{m.name}</b><br>'
                            f'{m.gross_margin_pct:.1f}% margin at {m.units_sold_monthly_avg:.0f} units/mo<br>'
                            f'Review pricing</div>', unsafe_allow_html=True)
        else:
            st.caption("None")

    with q4:
        st.markdown("**❌ Dead Weight**")
        dead_s = [m for m in skus if m.is_dead_stock]
        if dead_s:
            st.error(f"₹{sum(m.stock_value for m in dead_s):,.0f} locked")
            for m in dead_s:
                st.markdown(f'<div class="critical-flag"><b>{m.name}</b><br>'
                            f'{m.stock_on_hand:.0f} units · ₹{m.stock_value:,.0f}<br>'
                            f'No sale in {m.days_since_last_sale} days</div>', unsafe_allow_html=True)
        else:
            st.success("No dead stock")


# ══════════════════════════════════════════
# TAB 3 — RECEIVABLES
# ══════════════════════════════════════════
with tab3:
    st.markdown("### Receivables Health & Collections")

    ra, rb, rc, rd, re = st.columns(5)
    ra.metric("0–30 days", f"₹{receivables.bucket_0_30/1000:.0f}K")
    rb.metric("31–60 days", f"₹{receivables.bucket_31_60/1000:.0f}K")
    rc.metric("61–90 days", f"₹{receivables.bucket_61_90/1000:.0f}K",
              delta="Overdue" if receivables.bucket_61_90 > 0 else "", delta_color="inverse")
    rd.metric("90+ days", f"₹{receivables.bucket_90_plus/1000:.0f}K",
              delta="Critical" if receivables.bucket_90_plus > 0 else "", delta_color="inverse")
    re.metric("Collections Eff.", f"{receivables.collections_efficiency_pct:.0f}%",
              delta="target: 85%+", delta_color="off")

    st.divider()
    rch1, rch2 = st.columns(2)

    with rch1:
        st.markdown("#### Receivables Aging")
        st.caption("Money in the 90+ bucket is at high risk of becoming a bad debt.")
        st.bar_chart(pd.DataFrame({"Overdue ₹": aging_buckets_dict(receivables)}))

    with rch2:
        st.markdown("#### Outstanding by Customer")
        if receivables.party_overdue:
            party_chart = {po.party_name: po.total_outstanding for po in receivables.party_overdue}
            st.bar_chart(pd.DataFrame({"Outstanding ₹": party_chart}))

    st.divider()
    st.markdown("#### Overdue Register — Who Owes What")
    if receivables.party_overdue:
        party_rows = []
        for po in receivables.party_overdue:
            icon = {"Critical": "🔴", "High": "🟠", "Medium": "🟡", "Low": "🟢"}.get(po.risk_level, "⚪")
            party_rows.append({
                "Customer": po.party_name,
                "City": po.city,
                "Overdue ₹": int(po.total_outstanding),
                "Invoices": po.invoices_overdue,
                "Oldest (days)": po.oldest_overdue_days,
                "Ever Paid?": "✓ Yes" if po.has_ever_paid else "✗ Never",
                "Risk": f"{icon} {po.risk_level}",
            })
        st.dataframe(pd.DataFrame(party_rows), use_container_width=True, hide_index=True,
                     column_config={"Overdue ₹": st.column_config.NumberColumn(format="₹%d")})

    st.divider()
    st.markdown("#### Invoice-Level Overdue Detail")
    if receivables.overdue_invoices:
        inv_rows = []
        for oi in receivables.overdue_invoices:
            inv_rows.append({
                "Invoice #": oi.voucher_number,
                "Customer": oi.party_name,
                "Invoice Date": str(oi.invoice_date),
                "Due Date": str(oi.due_date),
                "Amount ₹": int(oi.invoice_amount),
                "Days Overdue": oi.days_overdue,
                "Bucket": oi.bucket,
            })
        df_inv = pd.DataFrame(inv_rows).sort_values("Days Overdue", ascending=False)
        st.dataframe(df_inv, use_container_width=True, hide_index=True,
                     column_config={"Amount ₹": st.column_config.NumberColumn(format="₹%d")})

    st.divider()
    st.markdown("#### Collections Summary (Last 90 Days)")
    ce1, ce2, ce3 = st.columns(3)
    ce1.metric("Invoices Raised", f"₹{receivables.invoices_raised_90d/1000:.0f}K")
    ce2.metric("Receipts Collected", f"₹{receivables.receipts_90d/1000:.0f}K")
    gap = receivables.invoices_raised_90d - receivables.receipts_90d
    ce3.metric("Collection Gap", f"₹{gap/1000:.0f}K",
               delta=f"{receivables.collections_efficiency_pct:.0f}% efficiency",
               delta_color="inverse" if gap > 0 else "normal")

    if receivables.anomaly_flags:
        st.divider()
        st.markdown("#### Receivables Anomaly Flags")
        for flag in receivables.anomaly_flags:
            box = "critical-flag" if "ZERO" in flag or "structural" in flag else "flag-box"
            st.markdown(f'<div class="{box}">{flag}</div>', unsafe_allow_html=True)


# ══════════════════════════════════════════
# TAB 4 — CASH FLOW
# ══════════════════════════════════════════
with tab4:
    st.markdown("### 30 / 60 / 90-Day Cash Flow Projection")
    st.caption("Built from actual outstanding invoices and payment probability per customer — not a spreadsheet formula.")

    cf1, cf2, cf3, cf4 = st.columns(4)
    cf1.metric("Projected Inflow (90d)", f"₹{cashflow.total_projected_inflow_90d/1000:.0f}K")
    cf2.metric("Projected Outflow (90d)", f"₹{cashflow.total_projected_outflow_90d/1000:.0f}K")
    cf3.metric("Net Cash (90d)", f"₹{cashflow.net_90d/1000:.0f}K",
               delta="Surplus" if cashflow.net_90d >= 0 else "Deficit",
               delta_color="normal" if cashflow.net_90d >= 0 else "inverse")
    cf4.metric("Working Capital", f"₹{cashflow.working_capital/100000:.1f}L",
               delta=cashflow.working_capital_trend, delta_color="off")

    st.divider()
    cfa, cfb = st.columns(2)

    with cfa:
        st.markdown("#### Inflow vs Outflow by Window")
        cf_df = pd.DataFrame({
            "Inflow ₹": [p.projected_inflow for p in cashflow.periods],
            "Outflow ₹": [p.projected_outflow for p in cashflow.periods],
        }, index=[p.label for p in cashflow.periods])
        st.bar_chart(cf_df)

    with cfb:
        st.markdown("#### Net Cash Position by Window")
        net_df = pd.DataFrame(
            {"Net ₹": [p.net for p in cashflow.periods]},
            index=[p.label for p in cashflow.periods]
        )
        st.bar_chart(net_df)

    st.divider()
    st.markdown("#### Period-by-Period Detail")
    pw1, pw2, pw3 = st.columns(3)
    conf_colors = {"High": "#28a745", "Medium": "#ffc107", "Low": "#dc3545"}

    for col, period in zip([pw1, pw2, pw3], cashflow.periods):
        with col:
            color = conf_colors.get(period.confidence, "#888")
            sign = "+" if period.net >= 0 else ""
            st.markdown(f"""
            <div style='border:2px solid {color};border-radius:10px;padding:16px;text-align:center;'>
                <div style='font-size:13px;color:#888;margin-bottom:8px;'>{period.label}</div>
                <div style='font-size:11px;color:#888;'>Projected inflow</div>
                <div style='font-size:20px;font-weight:600;color:#28a745;'>₹{period.projected_inflow:,.0f}</div>
                <div style='font-size:11px;color:#888;margin-top:8px;'>Projected outflow</div>
                <div style='font-size:20px;font-weight:600;color:#dc3545;'>₹{period.projected_outflow:,.0f}</div>
                <div style='border-top:1px solid #eee;margin:10px 0;'></div>
                <div style='font-size:22px;font-weight:700;color:{color};'>{sign}₹{period.net:,.0f}</div>
                <div style='font-size:11px;color:{color};margin-top:4px;'>Confidence: {period.confidence}</div>
            </div>""", unsafe_allow_html=True)

    st.divider()
    st.markdown("#### Working Capital Breakdown")
    wc1, wc2, wc3 = st.columns(3)
    wc1.metric("Total Receivables", f"₹{cashflow.total_receivables/100000:.1f}L")
    wc2.metric("Estimated Payables", f"₹{cashflow.total_payables/1000:.0f}K")
    wc3.metric("Net Working Capital", f"₹{cashflow.working_capital/100000:.1f}L",
               delta=cashflow.working_capital_trend,
               delta_color="normal" if cashflow.working_capital >= 0 else "inverse")

    if cashflow.stress_flags:
        st.divider()
        st.markdown("#### Cash Flow Stress Signals")
        for flag in cashflow.stress_flags:
            box = "critical-flag" if "⚠" in flag else "info-flag"
            st.markdown(f'<div class="{box}">{flag}</div>', unsafe_allow_html=True)

    st.divider()
    st.markdown("#### Historical Cash Activity (Last 90 Days)")
    ha1, ha2, ha3 = st.columns(3)
    ha1.metric("Actual Inflows", f"₹{cashflow.actual_inflows_90d/1000:.0f}K")
    ha2.metric("Actual Outflows", f"₹{cashflow.actual_outflows_90d/1000:.0f}K")
    net_hist = cashflow.actual_inflows_90d - cashflow.actual_outflows_90d
    ha3.metric("Historical Net", f"₹{net_hist/1000:.0f}K",
               delta="Positive" if net_hist >= 0 else "Negative",
               delta_color="normal" if net_hist >= 0 else "inverse")


# ══════════════════════════════════════════
# TAB 5 — ANOMALIES
# ══════════════════════════════════════════
with tab5:
    st.markdown("### Anomaly Detection & Audit Flags")
    st.caption("Pattern detection on your existing Tally entries. Things your accountant may have missed.")

    all_flags = []

    for flag in receivables.anomaly_flags:
        sev = "Critical" if "ZERO" in flag else "Warning" if "⚠" in flag else "Info"
        all_flags.append({"Source": "Receivables", "Severity": sev, "Flag": flag})

    for flag in cashflow.stress_flags:
        sev = "Warning" if "⚠" in flag else "Info"
        all_flags.append({"Source": "Cash Flow", "Severity": sev, "Flag": flag})

    for m in customers:
        if not m.has_ever_paid and m.outstanding_balance > 0 and m.order_count_12m > 1:
            all_flags.append({
                "Source": "Customer", "Severity": "Critical",
                "Flag": f"⚠ {m.name}: {m.order_count_12m} orders placed, ZERO payments received. Outstanding: ₹{m.outstanding_balance:,.0f}."
            })
        if m.dso_days > 120:
            all_flags.append({
                "Source": "Customer", "Severity": "Warning",
                "Flag": f"⚠ {m.name}: DSO of {m.dso_days:.0f} days — severely beyond credit terms."
            })

    for m in skus:
        if m.return_rate_pct > 8:
            all_flags.append({
                "Source": "Inventory", "Severity": "Warning",
                "Flag": f"⚠ {m.name}: {m.return_rate_pct:.1f}% return rate — investigate quality."
            })
        if m.is_dead_stock and m.stock_value > 10000:
            all_flags.append({
                "Source": "Inventory", "Severity": "Warning",
                "Flag": f"⚠ {m.name}: ₹{m.stock_value:,.0f} idle for {m.days_since_last_sale} days."
            })

    critical = [f for f in all_flags if f["Severity"] == "Critical"]
    warnings = [f for f in all_flags if f["Severity"] == "Warning"]
    infos = [f for f in all_flags if f["Severity"] == "Info"]

    an1, an2, an3, an4 = st.columns(4)
    an1.metric("Total Flags", str(len(all_flags)))
    an2.metric("🔴 Critical", str(len(critical)),
               delta="Immediate action" if critical else "None",
               delta_color="inverse" if critical else "off")
    an3.metric("🟡 Warnings", str(len(warnings)))
    an4.metric("ℹ Info", str(len(infos)))

    st.divider()

    # Filters
    af1, af2 = st.columns(2)
    with af1:
        sources = ["All"] + sorted(set(f["Source"] for f in all_flags))
        sel_src = st.selectbox("Filter by source", sources, key="anom_source")
    with af2:
        sel_sev = st.selectbox("Filter by severity", ["All", "Critical", "Warning", "Info"], key="anom_sev")

    filtered = all_flags
    if sel_src != "All":
        filtered = [f for f in filtered if f["Source"] == sel_src]
    if sel_sev != "All":
        filtered = [f for f in filtered if f["Severity"] == sel_sev]

    order = {"Critical": 0, "Warning": 1, "Info": 2}
    filtered.sort(key=lambda x: order.get(x["Severity"], 3))

    st.divider()
    if filtered:
        for f in filtered:
            box = "critical-flag" if f["Severity"] == "Critical" else "flag-box" if f["Severity"] == "Warning" else "info-flag"
            tag = f'<span style="font-size:10px;background:#e0e0e0;color:#333;padding:2px 6px;border-radius:10px;margin-right:6px;">{f["Source"]}</span>'
            st.markdown(f'<div class="{box}">{tag}{f["Flag"]}</div>', unsafe_allow_html=True)
    else:
        st.success("No flags match the selected filters.")

    st.divider()
    st.markdown("#### Action Priority")
    if critical:
        st.error(f"**{len(critical)} critical issue(s) — address today.**")
        for f in critical:
            st.markdown(f"- {f['Flag'].replace('⚠ ', '')}")
    if warnings:
        st.warning(f"**{len(warnings)} warning(s) — review this week.**")
    if infos:
        st.info(f"**{len(infos)} informational flag(s) — monitor.**")
    if not all_flags:
        st.success("No anomalies detected. Business data looks clean.")

    # Flag summary table
    if all_flags:
        st.divider()
        st.markdown("#### All Flags — Summary Table")
        st.dataframe(pd.DataFrame(all_flags), use_container_width=True, hide_index=True)


# ── Footer ────────────────────────────────
st.divider()
fc1, fc2, fc3 = st.columns(3)
fc1.caption("NerveSight Lite v0.1 · Demo mode")
fc2.caption(f"Data as of {date.today().strftime('%d %b %Y')} · Sample Tally dataset")
fc3.caption("Built by Shriman Maheshwari · BITS Pilani + CentraleSupélec")
