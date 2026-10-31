"""Remaining analysis sections 4-6 (Rule 3 exposure, Rule 4 missing, Income)."""
import pandas as pd
import numpy as np
import sys

DATA_DIR = "/Users/adhivp/CredX/core/data/raw"

print("[4/6] Loading for exposure analysis...", flush=True)
app = pd.read_csv(f"{DATA_DIR}/application_train.csv",
                   usecols=["SK_ID_CURR", "TARGET", "AMT_INCOME_TOTAL", "AMT_ANNUITY",
                            "EXT_SOURCE_1", "EXT_SOURCE_2", "EXT_SOURCE_3",
                            "OWN_CAR_AGE", "OCCUPATION_TYPE", "AMT_CREDIT", "AMT_GOODS_PRICE", "DAYS_EMPLOYED"])
bureau = pd.read_csv(f"{DATA_DIR}/bureau.csv",
                      usecols=["SK_ID_CURR", "SK_ID_BUREAU", "CREDIT_ACTIVE",
                                "AMT_CREDIT_SUM", "AMT_CREDIT_SUM_DEBT", "AMT_CREDIT_SUM_OVERDUE"])
cc_bal = pd.read_csv(f"{DATA_DIR}/credit_card_balance.csv",
                      usecols=["SK_ID_CURR", "MONTHS_BALANCE", "AMT_BALANCE", "AMT_CREDIT_LIMIT_ACTUAL"])
inst = pd.read_csv(f"{DATA_DIR}/installments_payments.csv", usecols=["SK_ID_CURR"])
pos_cash = pd.read_csv(f"{DATA_DIR}/POS_CASH_balance.csv", usecols=["SK_ID_CURR"])
print("  Loaded.", flush=True)

# RULE 3: EXPOSURE
print("\n" + "=" * 70, flush=True)
print("RULE 3: INCREASING EXPOSURE ANALYSIS", flush=True)
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

print("\n--- Default Rate by Active Loans ---", flush=True)
l_bins = [0, 1, 2, 3, 4, 5, 99]
l_labels = ["0-1", "2", "3", "4", "5", "6+"]
bureau_agg["l_bucket"] = pd.cut(bureau_agg["active_loans"], bins=l_bins, labels=l_labels, right=True)
for label, grp in bureau_agg.groupby("l_bucket", observed=True):
    print(f"  Active {label}: n={len(grp):,}, default={grp['TARGET'].mean():.4f}", flush=True)

print("\n--- Default Rate by Utilisation ---", flush=True)
u_bins = [0, 0.3, 0.5, 0.7, 0.85, 0.95, 1.01, 99]
u_labels = ["0-30%", "30-50%", "50-70%", "70-85%", "85-95%", "95-100%", "100%+"]
valid = bureau_agg[bureau_agg["utilisation"].notna() & (bureau_agg["utilisation"] >= 0)].copy()
valid["u_bucket"] = pd.cut(valid["utilisation"].clip(upper=5), bins=u_bins, labels=u_labels, right=True)
for label, grp in valid.groupby("u_bucket", observed=True):
    print(f"  Util {label}: n={len(grp):,}, default={grp['TARGET'].mean():.4f}", flush=True)

# Credit card util
print("\n--- CC Utilisation vs Default ---", flush=True)
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

# RULE 4: MISSING DATA
print("\n" + "=" * 70, flush=True)
print("RULE 4: MISSING DATA ANALYSIS", flush=True)
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
app_target = dict(zip(app["SK_ID_CURR"], app["TARGET"]))
for name, ids in [("No installments", all_ids - ids_inst), ("No bureau", all_ids - ids_bureau),
                   ("Both inst+bureau", ids_inst & ids_bureau), ("Neither", all_ids - ids_inst - ids_bureau)]:
    targets = [app_target[i] for i in ids if i in app_target]
    if targets:
        print(f"  {name}: n={len(targets):,}, default={np.mean(targets):.4f}", flush=True)

# Records per customer
inst_full = pd.read_csv(f"{DATA_DIR}/installments_payments.csv", usecols=["SK_ID_CURR"])
inst_counts = inst_full.groupby("SK_ID_CURR").size().reset_index(name="n")
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

# INCOME & OBLIGATIONS
print("\n" + "=" * 70, flush=True)
print("INCOME & OBLIGATION ANALYSIS", flush=True)
print("=" * 70, flush=True)

app["pti"] = app["AMT_ANNUITY"] / app["AMT_INCOME_TOTAL"]
pti = app["pti"].dropna()

print("\n--- PTI Percentiles ---", flush=True)
for p in [25, 50, 75, 90, 95, 99]:
    print(f"  P{p}: {pti.quantile(p/100):.4f}", flush=True)

print("\n--- Default Rate by PTI ---", flush=True)
pti_bins = [0, 0.1, 0.2, 0.3, 0.4, 0.5, 99]
pti_labels = ["0-10%", "10-20%", "20-30%", "30-40%", "40-50%", "50%+"]
app["pti_bucket"] = pd.cut(app["pti"], bins=pti_bins, labels=pti_labels, right=True)
for label, grp in app.groupby("pti_bucket", observed=True):
    print(f"  PTI {label}: n={len(grp):,}, default={grp['TARGET'].mean():.4f}", flush=True)

print("\n" + "=" * 70, flush=True)
print("ALL ANALYSIS COMPLETE", flush=True)
print("=" * 70, flush=True)
