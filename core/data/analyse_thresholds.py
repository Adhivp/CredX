"""
Statistical analysis of Home Credit dataset — optimized for large files.
"""
import pandas as pd
import numpy as np
import sys

DATA_DIR = "/Users/adhivp/CredX/core/data/raw"

print("=" * 70, flush=True)
print("HOME CREDIT DATASET - STATISTICAL ANALYSIS FOR EWS RULE THRESHOLDS", flush=True)
print("=" * 70, flush=True)

# =====================================================================
# 1. LOAD KEY TABLES
# =====================================================================
print("\n[1/6] Loading datasets...", flush=True)

app = pd.read_csv(f"{DATA_DIR}/application_train.csv",
                   usecols=["SK_ID_CURR", "TARGET", "AMT_INCOME_TOTAL", "AMT_CREDIT",
                            "AMT_ANNUITY", "AMT_GOODS_PRICE", "DAYS_EMPLOYED",
                            "EXT_SOURCE_1", "EXT_SOURCE_2", "EXT_SOURCE_3",
                            "OWN_CAR_AGE", "OCCUPATION_TYPE"])
print(f"  application_train: {len(app):,} rows", flush=True)

inst = pd.read_csv(f"{DATA_DIR}/installments_payments.csv")
print(f"  installments_payments: {len(inst):,} rows", flush=True)

bureau = pd.read_csv(f"{DATA_DIR}/bureau.csv",
                      usecols=["SK_ID_CURR", "SK_ID_BUREAU", "CREDIT_ACTIVE",
                                "AMT_CREDIT_SUM", "AMT_CREDIT_SUM_DEBT", "AMT_CREDIT_SUM_OVERDUE"])
print(f"  bureau: {len(bureau):,} rows", flush=True)

cc_bal = pd.read_csv(f"{DATA_DIR}/credit_card_balance.csv",
                      usecols=["SK_ID_CURR", "MONTHS_BALANCE", "AMT_BALANCE", "AMT_CREDIT_LIMIT_ACTUAL"])
print(f"  credit_card_balance: {len(cc_bal):,} rows", flush=True)

pos_cash = pd.read_csv(f"{DATA_DIR}/POS_CASH_balance.csv", usecols=["SK_ID_CURR"])
print(f"  POS_CASH_balance loaded (IDs only)", flush=True)

default_rate = app["TARGET"].mean()
print(f"\n  Overall default rate: {default_rate:.4f} ({app['TARGET'].sum():,} / {len(app):,})", flush=True)

# =====================================================================
# 2. REPAYMENT DELAYS (Rule 1)
# =====================================================================
print("\n" + "=" * 70, flush=True)
print("[2/6] RULE 1: REPAYMENT DELAYS ANALYSIS", flush=True)
print("=" * 70, flush=True)

print("  Computing days late...", flush=True)
inst["days_late"] = inst["DAYS_ENTRY_PAYMENT"] - inst["DAYS_INSTALMENT"]
inst["is_late"] = (inst["days_late"] > 0).astype(np.int8)
inst["payment_shortfall"] = inst["AMT_INSTALMENT"] - inst["AMT_PAYMENT"]
inst["is_short"] = (inst["payment_shortfall"] > 10).astype(np.int8)

print("  Aggregating per customer (vectorized)...", flush=True)
cust_repay = inst.groupby("SK_ID_CURR").agg(
    total_instalments=("is_late", "count"),
    late_count=("is_late", "sum"),
    short_count=("is_short", "sum"),
    max_days_late=("days_late", "max"),
).reset_index()

late_only = inst.loc[inst["days_late"] > 0, "days_late"]
cust_late_avg = inst[inst["days_late"] > 0].groupby("SK_ID_CURR")["days_late"].mean().rename("avg_days_late")
cust_repay = cust_repay.merge(cust_late_avg, on="SK_ID_CURR", how="left")
cust_repay["avg_days_late"] = cust_repay["avg_days_late"].fillna(0)
cust_repay["late_ratio"] = cust_repay["late_count"] / cust_repay["total_instalments"]
cust_repay = cust_repay.merge(app[["SK_ID_CURR", "TARGET"]], on="SK_ID_CURR", how="left")

print("\n--- Days Late Distribution (when late > 0) ---", flush=True)
print(f"  Late payments: {len(late_only):,} / {len(inst):,} ({len(late_only)/len(inst):.2%})", flush=True)
for p in [25, 50, 75, 90, 95, 99]:
    print(f"  P{p}: {late_only.quantile(p/100):.0f} days", flush=True)

print("\n--- Late Ratio per Customer ---", flush=True)
for p in [25, 50, 75, 90, 95, 99]:
    print(f"  P{p}: {cust_repay['late_ratio'].quantile(p/100):.4f}", flush=True)

print("\n--- Default Rate by Late Ratio ---", flush=True)
bins = [0, 0.05, 0.10, 0.20, 0.30, 0.50, 1.01]
labels = ["0-5%", "5-10%", "10-20%", "20-30%", "30-50%", "50-100%"]
cust_repay["late_bucket"] = pd.cut(cust_repay["late_ratio"], bins=bins, labels=labels, right=False)
for label, grp in cust_repay.groupby("late_bucket", observed=True):
    print(f"  {label}: n={len(grp):,}, default={grp['TARGET'].mean():.4f}", flush=True)

print("\n--- Default Rate by Max DPD ---", flush=True)
dpd_bins = [0, 1, 15, 30, 60, 90, 180, 99999]
dpd_labels = ["0", "1-15", "16-30", "31-60", "61-90", "91-180", "180+"]
cust_repay["dpd_bucket"] = pd.cut(cust_repay["max_days_late"], bins=dpd_bins, labels=dpd_labels, right=True)
for label, grp in cust_repay.groupby("dpd_bucket", observed=True):
    print(f"  {label}: n={len(grp):,}, default={grp['TARGET'].mean():.4f}", flush=True)

print("\n--- Default Rate by Avg Days Late ---", flush=True)
avg_bins = [0, 5, 10, 15, 30, 60, 99999]
avg_labels = ["0-5d", "5-10d", "10-15d", "15-30d", "30-60d", "60d+"]
cust_repay["avg_bucket"] = pd.cut(cust_repay["avg_days_late"], bins=avg_bins, labels=avg_labels, right=True)
for label, grp in cust_repay.groupby("avg_bucket", observed=True):
    print(f"  {label}: n={len(grp):,}, default={grp['TARGET'].mean():.4f}", flush=True)

# =====================================================================
# 3. CONSECUTIVE STREAKS & RECOVERY (Rule 2) — sampled
# =====================================================================
print("\n" + "=" * 70, flush=True)
print("[3/6] RULE 2: TEMPORARY vs SUSTAINED ANALYSIS (sampled 30k)", flush=True)
print("=" * 70, flush=True)

sample_ids = cust_repay.sample(min(30000, len(cust_repay)), random_state=42)["SK_ID_CURR"].values
inst_s = inst[inst["SK_ID_CURR"].isin(set(sample_ids))].sort_values(["SK_ID_CURR", "DAYS_INSTALMENT"])

print("  Computing consecutive streaks...", flush=True)
streaks = []
recoveries = []
for cid, grp in inst_s.groupby("SK_ID_CURR"):
    is_late = (grp["days_late"].values > 0)
    # max consecutive
    mx, cur = 0, 0
    for v in is_late:
        if v:
            cur += 1
            if cur > mx: mx = cur
        else:
            cur = 0
    streaks.append({"SK_ID_CURR": cid, "max_streak": mx})
    # recovery rate
    rec, lc = 0, 0
    for i in range(len(is_late) - 1):
        if is_late[i]:
            lc += 1
            if not is_late[i + 1]:
                rec += 1
    recoveries.append({"SK_ID_CURR": cid, "recovery_rate": rec / lc if lc > 0 else 1.0})

streak_df = pd.DataFrame(streaks).merge(pd.DataFrame(recoveries), on="SK_ID_CURR")
streak_df = streak_df.merge(app[["SK_ID_CURR", "TARGET"]], on="SK_ID_CURR")
print("  Done.", flush=True)

print("\n--- Default Rate by Max Consecutive Late Streak ---", flush=True)
s_bins = [-1, 0, 1, 2, 3, 5, 10, 9999]
s_labels = ["0", "1", "2", "3", "4-5", "6-10", "10+"]
streak_df["s_bucket"] = pd.cut(streak_df["max_streak"], bins=s_bins, labels=s_labels, right=True)
for label, grp in streak_df.groupby("s_bucket", observed=True):
    print(f"  Streak {label}: n={len(grp):,}, default={grp['TARGET'].mean():.4f}", flush=True)

print("\n--- Default Rate by Recovery Rate ---", flush=True)
r_bins = [0, 0.2, 0.4, 0.6, 0.8, 1.01]
r_labels = ["0-20%", "20-40%", "40-60%", "60-80%", "80-100%"]
streak_df["r_bucket"] = pd.cut(streak_df["recovery_rate"], bins=r_bins, labels=r_labels, right=True)
for label, grp in streak_df.groupby("r_bucket", observed=True):
    print(f"  Recovery {label}: n={len(grp):,}, default={grp['TARGET'].mean():.4f}", flush=True)

# =====================================================================
# 4. EXPOSURE ANALYSIS (Rule 3)
# =====================================================================
print("\n" + "=" * 70, flush=True)
print("[4/6] RULE 3: INCREASING EXPOSURE ANALYSIS", flush=True)
print("=" * 70, flush=True)

bureau_agg = bureau.groupby("SK_ID_CURR").agg(
    active_loans=("CREDIT_ACTIVE", lambda x: (x == "Active").sum()),
    total_debt=("AMT_CREDIT_SUM_DEBT", "sum"),
    total_credit=("AMT_CREDIT_SUM", "sum"),
    total_overdue=("AMT_CREDIT_SUM_OVERDUE", "sum"),
).reset_index()
bureau_agg["utilisation"] = bureau_agg["total_debt"] / bureau_agg["total_credit"].replace(0, np.nan)
bureau_agg = bureau_agg.merge(app[["SK_ID_CURR", "TARGET", "AMT_INCOME_TOTAL"]], on="SK_ID_CURR", how="left")
bureau_agg["dti"] = bureau_agg["total_debt"] / (bureau_agg["AMT_INCOME_TOTAL"] * 12).replace(0, np.nan)

print("\n--- Active Loan Count Percentiles ---", flush=True)
for p in [25, 50, 75, 90, 95, 99]:
    print(f"  P{p}: {bureau_agg['active_loans'].quantile(p/100):.0f}", flush=True)

print("\n--- Default Rate by Active Loans ---", flush=True)
l_bins = [-1, 0, 1, 2, 3, 4, 99]
l_labels = ["0", "1", "2", "3", "4+"]
bureau_agg["l_bucket"] = pd.cut(bureau_agg["active_loans"], bins=l_bins, labels=l_labels, right=True)
for label, grp in bureau_agg.groupby("l_bucket", observed=True):
    print(f"  Active {label}: n={len(grp):,}, default={grp['TARGET'].mean():.4f}", flush=True)

print("\n--- Default Rate by Utilisation ---", flush=True)
u_bins = [0, 0.3, 0.5, 0.7, 0.85, 0.95, 1.01, 99]
u_labels = ["0-30%", "30-50%", "50-70%", "70-85%", "85-95%", "95-100%", "100%+"]
valid = bureau_agg[bureau_agg["utilisation"].notna() & (bureau_agg["utilisation"] >= 0)]
valid = valid.copy()
valid["u_bucket"] = pd.cut(valid["utilisation"].clip(upper=5), bins=u_bins, labels=u_labels, right=True)
for label, grp in valid.groupby("u_bucket", observed=True):
    print(f"  Util {label}: n={len(grp):,}, default={grp['TARGET'].mean():.4f}", flush=True)

# Credit card utilisation
print("\n--- Credit Card Utilisation ---", flush=True)
cc_latest = cc_bal.sort_values("MONTHS_BALANCE").groupby("SK_ID_CURR").last().reset_index()
cc_latest["cc_util"] = cc_latest["AMT_BALANCE"] / cc_latest["AMT_CREDIT_LIMIT_ACTUAL"].replace(0, np.nan)
cc_m = cc_latest.merge(app[["SK_ID_CURR", "TARGET"]], on="SK_ID_CURR", how="left")
cc_valid = cc_m[cc_m["cc_util"].notna() & (cc_m["cc_util"] >= 0)].copy()
cc_valid["cc_bucket"] = pd.cut(cc_valid["cc_util"].clip(upper=5), bins=u_bins, labels=u_labels, right=True)
for label, grp in cc_valid.groupby("cc_bucket", observed=True):
    print(f"  CC {label}: n={len(grp):,}, default={grp['TARGET'].mean():.4f}", flush=True)

# DTI
print("\n--- Default Rate by Debt-to-Annual-Income ---", flush=True)
dti_bins = [0, 0.2, 0.4, 0.6, 0.8, 1.0, 99]
dti_labels = ["0-20%", "20-40%", "40-60%", "60-80%", "80-100%", "100%+"]
dti_valid = bureau_agg[bureau_agg["dti"].notna() & (bureau_agg["dti"] >= 0)].copy()
dti_valid["dti_bucket"] = pd.cut(dti_valid["dti"].clip(upper=5), bins=dti_bins, labels=dti_labels, right=True)
for label, grp in dti_valid.groupby("dti_bucket", observed=True):
    print(f"  DTI {label}: n={len(grp):,}, default={grp['TARGET'].mean():.4f}", flush=True)

# =====================================================================
# 5. MISSING DATA (Rule 4)
# =====================================================================
print("\n" + "=" * 70, flush=True)
print("[5/6] RULE 4: MISSING DATA ANALYSIS", flush=True)
print("=" * 70, flush=True)

all_ids = set(app["SK_ID_CURR"].unique())
ids_inst = set(inst["SK_ID_CURR"].unique())
ids_bureau = set(bureau["SK_ID_CURR"].unique())
ids_cc = set(cc_bal["SK_ID_CURR"].unique())
ids_pos = set(pos_cash["SK_ID_CURR"].unique())

print(f"\n--- Data Availability ---", flush=True)
print(f"  Total customers: {len(all_ids):,}", flush=True)
for name, ids in [("installments", ids_inst), ("bureau", ids_bureau), ("credit_card", ids_cc), ("POS_cash", ids_pos)]:
    overlap = len(all_ids & ids)
    print(f"  {name}: {overlap:,} ({overlap/len(all_ids):.1%})", flush=True)

print(f"\n--- Default Rate by Data Availability ---", flush=True)
app_idx = app.set_index("SK_ID_CURR")["TARGET"]
for name, ids in [("No installments", all_ids - ids_inst), ("No bureau", all_ids - ids_bureau),
                   ("Both inst+bureau", ids_inst & ids_bureau), ("Neither", all_ids - ids_inst - ids_bureau)]:
    subset = list(ids)
    if subset:
        dr = app_idx.reindex(subset).mean()
        print(f"  {name}: n={len(subset):,}, default={dr:.4f}", flush=True)

# Records per customer
inst_counts = inst.groupby("SK_ID_CURR").size().reset_index(name="n")
inst_counts = inst_counts.merge(app[["SK_ID_CURR", "TARGET"]], on="SK_ID_CURR")
print(f"\n--- Instalment Records per Customer ---", flush=True)
for p in [10, 25, 50, 75, 90]:
    print(f"  P{p}: {inst_counts['n'].quantile(p/100):.0f}", flush=True)

print("\n--- Default Rate by Record Count ---", flush=True)
ic_bins = [0, 3, 6, 12, 24, 99999]
ic_labels = ["1-3", "4-6", "7-12", "13-24", "24+"]
inst_counts["ic_bucket"] = pd.cut(inst_counts["n"], bins=ic_bins, labels=ic_labels, right=True)
for label, grp in inst_counts.groupby("ic_bucket", observed=True):
    print(f"  Records {label}: n={len(grp):,}, default={grp['TARGET'].mean():.4f}", flush=True)

# Missing key columns
print(f"\n--- Key Column Missing Rates ---", flush=True)
for col in ["AMT_INCOME_TOTAL", "AMT_CREDIT", "AMT_ANNUITY", "AMT_GOODS_PRICE",
            "DAYS_EMPLOYED", "EXT_SOURCE_1", "EXT_SOURCE_2", "EXT_SOURCE_3",
            "OWN_CAR_AGE", "OCCUPATION_TYPE"]:
    if col in app.columns:
        print(f"  {col}: {app[col].isna().mean():.2%}", flush=True)

# =====================================================================
# 6. INCOME & OBLIGATIONS
# =====================================================================
print("\n" + "=" * 70, flush=True)
print("[6/6] INCOME & OBLIGATION ANALYSIS", flush=True)
print("=" * 70, flush=True)

app["pti"] = app["AMT_ANNUITY"] / app["AMT_INCOME_TOTAL"]

print("\n--- Payment-to-Income Ratio Percentiles ---", flush=True)
pti = app["pti"].dropna()
for p in [25, 50, 75, 90, 95, 99]:
    print(f"  P{p}: {pti.quantile(p/100):.4f}", flush=True)

print("\n--- Default Rate by PTI ---", flush=True)
pti_bins = [0, 0.1, 0.2, 0.3, 0.4, 0.5, 99]
pti_labels = ["0-10%", "10-20%", "20-30%", "30-40%", "40-50%", "50%+"]
app["pti_bucket"] = pd.cut(app["pti"], bins=pti_bins, labels=pti_labels, right=True)
for label, grp in app.groupby("pti_bucket", observed=True):
    print(f"  PTI {label}: n={len(grp):,}, default={grp['TARGET'].mean():.4f}", flush=True)

print("\n" + "=" * 70, flush=True)
print("ANALYSIS COMPLETE", flush=True)
print("=" * 70, flush=True)
