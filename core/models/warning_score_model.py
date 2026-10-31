"""
Model 2: Early Warning Score Model (XGBoost), trained on Home Credit data only.

The Home Credit tables are first converted into the shape the CredX platform ingests
(one row per instalment, one row per credit facility, a monthly income), and the
features are then computed by the SAME functions at training time and at scoring
time (instalment_features / facility_features). No synthetic or simulated inputs.

  installments_payments.csv            -> instalments (due, days late, amount due / paid)
  bureau.csv + credit_card_balance.csv -> facilities (active, sanctioned, outstanding, revolving)
  application_train.csv                -> monthly income, monthly payment, TARGET (default label)

Output: a calibrated default probability p. The 0-100 warning score maps p relative
to the population baseline (8.07%) onto the same lift bands rules.md uses:
  lift < 1.15 -> Low (0-20), 1.15-1.35 -> Medium (20-45),
  1.35-1.65 -> High (45-70), > 1.65 -> Critical (70-100, 100 at 4x baseline).

Train:  python -m core.models.warning_score_model   (from the repository root)
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import joblib
import numpy as np
import pandas as pd

MODEL_DIR = Path(__file__).parent / "saved"
MODEL_PATH = MODEL_DIR / "warning_score_model.joblib"
METRICS_PATH = MODEL_DIR / "warning_score_model.metrics.json"

BASELINE_DEFAULT_RATE = 0.0807  # Home Credit application_train: 24,825 / 307,511
RECENT_WINDOW = 6  # instalments
REVOLVING_TYPES = {"credit_card", "credit card", "overdraft", "revolving"}

FEATURE_COLS = [
    # Repayment behaviour (one row per instalment)
    "n_instalments",
    "late_ratio",
    "late_ratio_recent",
    "severe_late_ratio",
    "max_days_late",
    "avg_days_late",
    "max_late_streak",
    "recovery_rate",
    "underpaid_ratio",
    "shortfall_ratio",
    "days_late_trend",
    "history_span_months",
    "loan_count",
    "instalment_to_income",
    # Exposure (one row per credit facility)
    "active_loans",
    "utilisation_ratio",
    "revolving_utilisation",
    # Affordability (same-period monthly amounts)
    "debt_to_income",
    "payment_to_income",
]

FEATURE_LABELS = {
    "n_instalments": "length of repayment history",
    "late_ratio": "share of late payments",
    "late_ratio_recent": "late payments in the last 6 instalments",
    "severe_late_ratio": "payments more than 30 days late",
    "max_days_late": "worst days past due",
    "avg_days_late": "average days late",
    "max_late_streak": "longest run of late payments",
    "recovery_rate": "recovery after missed payments",
    "underpaid_ratio": "partial payments",
    "shortfall_ratio": "unpaid share of amounts due",
    "days_late_trend": "lateness getting worse recently",
    "history_span_months": "months of repayment history",
    "loan_count": "number of loans repaid",
    "instalment_to_income": "instalment size relative to income",
    "active_loans": "number of active loans",
    "utilisation_ratio": "credit utilisation",
    "revolving_utilisation": "card / overdraft utilisation",
    "debt_to_income": "debt relative to income",
    "payment_to_income": "instalments relative to income",
}

INSTALMENT_FEATURES = [
    "n_instalments", "late_ratio", "late_ratio_recent", "severe_late_ratio", "max_days_late",
    "avg_days_late", "max_late_streak", "recovery_rate", "underpaid_ratio", "shortfall_ratio",
    "days_late_trend", "history_span_months", "loan_count",
]

# lift (p / baseline) -> score knots, matching the rules.md multiplier bands
_LIFT_KNOTS = [0.0, 1.15, 1.35, 1.65, 4.0]
_SCORE_KNOTS = [0.0, 20.0, 45.0, 70.0, 100.0]


@dataclass
class WarningScoreResult:
    customer_id: str
    warning_score: float  # 0-100, higher = more risk
    risk_category: str  # Low, Medium, High, Critical
    default_probability: float  # calibrated, 0-1
    lift: float  # default_probability / baseline
    top_contributors: List[dict]  # [{feature, label, value, contribution}]
    rule_scores: dict  # kept for backward compatibility (core/ews_engine.py)
    indicators: List[str]


# ── Shared feature code (training and scoring) ─────────────────────────────


def instalment_features(inst: pd.DataFrame) -> pd.DataFrame:
    """One row per instalment -> per-customer behaviour features.

    Columns: customer_id, loan_id, due (days, numeric), days_late (> 0 = late),
    amount_due, amount_paid.
    """
    if inst.empty:
        return pd.DataFrame(columns=INSTALMENT_FEATURES + ["mean_amount_due"]).rename_axis("customer_id")

    df = inst[["customer_id", "loan_id", "due", "days_late", "amount_due", "amount_paid"]].copy()
    df["days_late"] = pd.to_numeric(df["days_late"], errors="coerce").fillna(0).clip(lower=0)
    df = df.sort_values(["customer_id", "due"], kind="mergesort").reset_index(drop=True)

    cid = df["customer_id"]
    late = df["days_late"] > 0
    same_next = cid.eq(cid.shift(-1))
    next_late = late.shift(-1, fill_value=False)

    # Run-length of consecutive late instalments per customer
    run_id = (late.ne(late.shift()) | cid.ne(cid.shift())).cumsum()
    streak = df.groupby(run_id).cumcount() + 1

    from_end = df.groupby("customer_id").cumcount(ascending=False)
    due_amt = df["amount_due"].fillna(0).clip(lower=0)
    paid_amt = df["amount_paid"].fillna(0).clip(lower=0)

    work = pd.DataFrame({
        "customer_id": cid,
        "late": late.astype(float),
        "late_recent": late.astype(float).where(from_end < RECENT_WINDOW),
        "dl_recent": df["days_late"].where(from_end < RECENT_WINDOW),
        "dl_before": df["days_late"].where(from_end >= RECENT_WINDOW),
        "due": df["due"],
        "loan_id": df["loan_id"],
        "severe": (df["days_late"] > 30).astype(float),
        "days_late": df["days_late"],
        "days_late_if_late": df["days_late"].where(late),
        "streak": streak.where(late, 0),
        "can_recover": (late & same_next).astype(float),
        "recovered": (late & same_next & ~next_late).astype(float),
        "underpaid": ((paid_amt < 0.95 * due_amt) & (due_amt > 0)).astype(float),
        "due_amt": due_amt,
        "paid_amt": paid_amt,
    })
    g = work.groupby("customer_id")
    out = pd.DataFrame({
        "n_instalments": g.size().astype(float),
        "late_ratio": g["late"].mean(),
        "late_ratio_recent": g["late_recent"].mean(),
        "severe_late_ratio": g["severe"].mean(),
        "max_days_late": g["days_late"].max(),
        "avg_days_late": g["days_late_if_late"].mean().fillna(0),
        "max_late_streak": g["streak"].max().astype(float),
        "underpaid_ratio": g["underpaid"].mean(),
        "days_late_trend": g["dl_recent"].mean() - g["dl_before"].mean(),  # NaN if < 7 instalments
        "history_span_months": (g["due"].max() - g["due"].min()) / 30.0,
        "loan_count": g["loan_id"].nunique().astype(float),
        "mean_amount_due": g["due_amt"].mean(),
    })
    recov = g["recovered"].sum() / g["can_recover"].sum().replace(0, np.nan)
    out["recovery_rate"] = recov.fillna(1.0)  # no misses -> nothing to recover from
    due_sum = g["due_amt"].sum().replace(0, np.nan)
    out["shortfall_ratio"] = (1 - g["paid_amt"].sum() / due_sum).clip(0, 1).fillna(0)
    return out


def facility_features(fac: pd.DataFrame) -> pd.DataFrame:
    """One row per credit facility -> per-customer exposure features.

    Columns: customer_id, active (bool), sanctioned, outstanding, revolving (bool), credit_limit.
    """
    cols = ["active_loans", "utilisation_ratio", "revolving_utilisation", "total_outstanding"]
    if fac.empty:
        return pd.DataFrame(columns=cols).rename_axis("customer_id")
    df = fac.copy()
    df["outstanding"] = df["outstanding"].fillna(0).clip(lower=0)
    df["sanctioned"] = df["sanctioned"].fillna(0).clip(lower=0)
    act = df[df["active"]]
    g_act = act.groupby("customer_id")
    out = pd.DataFrame(index=pd.Index(df["customer_id"].unique(), name="customer_id"))
    out["active_loans"] = g_act.size().reindex(out.index).fillna(0).astype(float)
    util = g_act["outstanding"].sum() / g_act["sanctioned"].sum().replace(0, np.nan)
    out["utilisation_ratio"] = util.reindex(out.index).clip(0, 2)
    rev = act[act["revolving"] & (act["credit_limit"].fillna(0) > 0)]
    g_rev = rev.groupby("customer_id")
    rev_util = g_rev["outstanding"].sum() / g_rev["credit_limit"].sum()
    out["revolving_utilisation"] = rev_util.reindex(out.index).clip(0, 2)
    out["total_outstanding"] = g_act["outstanding"].sum().reindex(out.index).fillna(0)
    return out


def assemble_features(
    inst_feats: pd.DataFrame,
    fac_feats: pd.DataFrame,
    monthly_income: pd.Series,
    monthly_payment: pd.Series,
    index: pd.Index,
) -> pd.DataFrame:
    """Join behaviour + exposure + affordability into FEATURE_COLS (NaN = unknown)."""
    X = pd.DataFrame(index=index).join(inst_feats, how="left").join(fac_feats, how="left")
    X["n_instalments"] = X["n_instalments"].fillna(0)
    income = monthly_income.reindex(index).where(lambda s: s > 0)
    X["debt_to_income"] = (X["total_outstanding"] / income).clip(upper=100)
    X["payment_to_income"] = (monthly_payment.reindex(index) / income).clip(upper=10)
    X["instalment_to_income"] = (X["mean_amount_due"] / income).clip(upper=10)
    return X[FEATURE_COLS].astype(float)


# ── Home Credit -> platform shape ──────────────────────────────────────────


def build_training_features(data_dir: str, n_customers: Optional[int] = None, seed: int = 42) -> pd.DataFrame:
    """Convert Home Credit into platform-shaped tables and compute FEATURE_COLS + TARGET."""
    d = Path(data_dir)
    t0 = time.time()

    def log(msg: str) -> None:
        print(f"  [{time.time() - t0:5.0f}s] {msg}", flush=True)

    app = pd.read_csv(d / "application_train.csv",
                      usecols=["SK_ID_CURR", "TARGET", "AMT_INCOME_TOTAL", "AMT_ANNUITY"])
    if n_customers:
        app = app.sample(min(n_customers, len(app)), random_state=seed)
    ids = pd.Index(app["SK_ID_CURR"].values, name="customer_id")
    log(f"applications: {len(app):,} customers, default rate {app['TARGET'].mean():.2%}")

    # Instalments: merge split payments into one row per (loan, instalment number)
    inst = pd.read_csv(
        d / "installments_payments.csv",
        usecols=["SK_ID_PREV", "SK_ID_CURR", "NUM_INSTALMENT_NUMBER", "DAYS_INSTALMENT",
                 "DAYS_ENTRY_PAYMENT", "AMT_INSTALMENT", "AMT_PAYMENT"],
        dtype={"SK_ID_PREV": "int32", "SK_ID_CURR": "int32", "NUM_INSTALMENT_NUMBER": "int16",
               "DAYS_INSTALMENT": "float32", "DAYS_ENTRY_PAYMENT": "float32",
               "AMT_INSTALMENT": "float32", "AMT_PAYMENT": "float32"},
    )
    inst = inst[inst["SK_ID_CURR"].isin(ids)]
    log(f"instalment payment rows: {len(inst):,}")
    inst = inst.groupby(["SK_ID_CURR", "SK_ID_PREV", "NUM_INSTALMENT_NUMBER"], sort=False).agg(
        due=("DAYS_INSTALMENT", "min"),
        last_payment=("DAYS_ENTRY_PAYMENT", "max"),
        amount_due=("AMT_INSTALMENT", "max"),
        amount_paid=("AMT_PAYMENT", "sum"),
    ).reset_index()
    # No payment entry recorded: count it as 30 days late (missed instalment)
    inst["days_late"] = (inst["last_payment"] - inst["due"]).fillna(30)
    inst = inst.rename(columns={"SK_ID_CURR": "customer_id", "SK_ID_PREV": "loan_id"})
    log(f"instalments (platform shape): {len(inst):,}")
    inst_feats = instalment_features(inst)
    del inst
    log("behaviour features done")

    # Facilities: bureau credits + Home Credit's own cards (latest month per card)
    bureau = pd.read_csv(d / "bureau.csv", usecols=[
        "SK_ID_CURR", "CREDIT_ACTIVE", "AMT_CREDIT_SUM", "AMT_CREDIT_SUM_DEBT", "CREDIT_TYPE"])
    bureau = bureau[bureau["SK_ID_CURR"].isin(ids)]
    revolving = bureau["CREDIT_TYPE"].eq("Credit card")
    fac_b = pd.DataFrame({
        "customer_id": bureau["SK_ID_CURR"].values,
        "active": bureau["CREDIT_ACTIVE"].eq("Active").values,
        "sanctioned": bureau["AMT_CREDIT_SUM"].values,
        "outstanding": bureau["AMT_CREDIT_SUM_DEBT"].values,
        "revolving": revolving.values,
        "credit_limit": np.where(revolving, bureau["AMT_CREDIT_SUM"], np.nan),
    })
    cc = pd.read_csv(d / "credit_card_balance.csv", usecols=[
        "SK_ID_PREV", "SK_ID_CURR", "MONTHS_BALANCE", "AMT_BALANCE", "AMT_CREDIT_LIMIT_ACTUAL"])
    cc = cc[cc["SK_ID_CURR"].isin(ids)]
    cc = cc.sort_values("MONTHS_BALANCE").groupby("SK_ID_PREV").last().reset_index()
    fac_c = pd.DataFrame({
        "customer_id": cc["SK_ID_CURR"].values,
        "active": (cc["AMT_CREDIT_LIMIT_ACTUAL"] > 0).values,
        "sanctioned": cc["AMT_CREDIT_LIMIT_ACTUAL"].values,
        "outstanding": cc["AMT_BALANCE"].values,
        "revolving": True,
        "credit_limit": cc["AMT_CREDIT_LIMIT_ACTUAL"].values,
    })
    fac_feats = facility_features(pd.concat([fac_b, fac_c], ignore_index=True))
    log(f"facilities: {len(fac_b) + len(fac_c):,}; exposure features done")

    a = app.set_index("SK_ID_CURR")
    X = assemble_features(inst_feats, fac_feats, a["AMT_INCOME_TOTAL"], a["AMT_ANNUITY"], ids)
    X["TARGET"] = a["TARGET"].reindex(ids).values
    log(f"feature matrix: {X.shape}")
    return X


# ── Platform (CredX DB) -> features ────────────────────────────────────────


def platform_features(
    repayments: pd.DataFrame,
    exposure: pd.DataFrame,
    monthly_income: Dict[str, float],
) -> pd.DataFrame:
    """Features for CredX customers, using the same code path as training.

    repayments: customer_id, loan_id, due_date, dpd, amount_due, amount_paid (de-duplicated)
    exposure:   customer_id, facility_type, sanctioned_amount, credit_limit,
                outstanding_balance, instalment_amount
    monthly_income: customer_id -> average monthly cash-flow inflow
    """
    ids = set(map(str, monthly_income))
    if not repayments.empty:
        ids |= set(repayments["customer_id"].astype(str))
    if not exposure.empty:
        ids |= set(exposure["customer_id"].astype(str))
    index = pd.Index(sorted(ids), name="customer_id")

    if not repayments.empty:
        due = pd.to_datetime(repayments["due_date"], errors="coerce")
        inst_feats = instalment_features(pd.DataFrame({
            "customer_id": repayments["customer_id"].astype(str),
            "loan_id": repayments["loan_id"].astype(str),
            # Same unit as Home Credit's DAYS_INSTALMENT: days
            "due": (due - pd.Timestamp("1970-01-01")).dt.days.astype(float),
            "days_late": repayments["dpd"],
            "amount_due": repayments["amount_due"],
            "amount_paid": repayments["amount_paid"],
        }))
    else:
        inst_feats = instalment_features(pd.DataFrame())

    payment = pd.Series(dtype=float)
    if not exposure.empty:
        ftype = exposure["facility_type"].astype(str).str.lower()
        limit = exposure["credit_limit"].where(exposure["credit_limit"] > 0)
        fac_feats = facility_features(pd.DataFrame({
            "customer_id": exposure["customer_id"].astype(str),
            "active": exposure["outstanding_balance"].fillna(0) > 0,
            "sanctioned": exposure["sanctioned_amount"],
            "outstanding": exposure["outstanding_balance"],
            "revolving": ftype.isin(REVOLVING_TYPES),
            "credit_limit": limit.fillna(exposure["sanctioned_amount"]),
        }))
        active = exposure[exposure["outstanding_balance"].fillna(0) > 0]
        payment = active.groupby(active["customer_id"].astype(str))["instalment_amount"].sum()
    else:
        fac_feats = facility_features(pd.DataFrame())

    if not repayments.empty:
        # No exposure snapshot: use the latest amount due per loan as the monthly payment
        last_due = (repayments.assign(customer_id=repayments["customer_id"].astype(str))
                    .sort_values("due_date").groupby(["customer_id", "loan_id"])["amount_due"].last()
                    .groupby(level="customer_id").sum())
        payment = payment.reindex(index).fillna(last_due.reindex(index))

    income = pd.Series({str(k): v for k, v in monthly_income.items()}, dtype=float)
    return assemble_features(inst_feats, fac_feats, income, payment.reindex(index), index)


# ── Training ───────────────────────────────────────────────────────────────


def lift_to_score(lift: np.ndarray) -> np.ndarray:
    return np.interp(lift, _LIFT_KNOTS, _SCORE_KNOTS)


def score_to_category(score: float) -> str:
    if score < 20:
        return "Low"
    if score < 45:
        return "Medium"
    if score < 70:
        return "High"
    return "Critical"


def train_warning_score_model(data_dir: str, n_customers: Optional[int] = None) -> dict:
    from sklearn.metrics import brier_score_loss, roc_auc_score
    from sklearn.model_selection import train_test_split
    from xgboost import XGBClassifier

    print("=" * 60 + "\nWarning-score model: Home Credit only\n" + "=" * 60, flush=True)
    df = build_training_features(data_dir, n_customers)
    X, y = df[FEATURE_COLS], df["TARGET"].astype(int).values

    X_tmp, X_test, y_tmp, y_test = train_test_split(X, y, test_size=0.15, random_state=42, stratify=y)
    X_train, X_val, y_train, y_val = train_test_split(
        X_tmp, y_tmp, test_size=0.1765, random_state=42, stratify=y_tmp)  # 70 / 15 / 15

    # No class re-weighting: keep probabilities calibrated so the lift bands mean what they say.
    params = {"learning_rate": 0.03, "max_depth": 5, "min_child_weight": 50,
              "subsample": 0.8, "colsample_bytree": 0.8, "reg_lambda": 2.0}
    model = XGBClassifier(
        n_estimators=3000, tree_method="hist", eval_metric=["logloss", "auc"],
        early_stopping_rounds=150, random_state=42, n_jobs=-1, **params,
    )
    model.fit(X_train, y_train, eval_set=[(X_val, y_val)], verbose=250)

    p_test = model.predict_proba(X_test)[:, 1]
    auc = float(roc_auc_score(y_test, p_test))
    scores = lift_to_score(p_test / BASELINE_DEFAULT_RATE)
    cats = np.array([score_to_category(s) for s in scores])

    bands = {}
    for c in ["Low", "Medium", "High", "Critical"]:
        m = cats == c
        bands[c] = {
            "share": round(float(m.mean()), 4),
            "observed_default_rate": round(float(y_test[m].mean()), 4) if m.any() else None,
        }
    dec = pd.qcut(p_test, 10, labels=False, duplicates="drop")
    calibration = [
        {"decile": int(k) + 1, "predicted": round(float(p_test[dec == k].mean()), 4),
         "observed": round(float(y_test[dec == k].mean()), 4)}
        for k in sorted(set(dec))
    ]
    gain = model.get_booster().get_score(importance_type="gain")
    importance = sorted(
        ({"feature": f, "gain": round(float(gain.get(f, 0.0)), 2)} for f in FEATURE_COLS),
        key=lambda r: -r["gain"],
    )

    metrics = {
        "dataset": "Home Credit Default Risk (Kaggle): application_train, installments_payments, "
                   "bureau, credit_card_balance",
        "customers": int(len(df)),
        "split_train_validation_test": [int(len(X_train)), int(len(X_val)), int(len(X_test))],
        "baseline_default_rate": round(float(y.mean()), 4),
        "algorithm": "XGBoost XGBClassifier (hist)",
        "trees": int(model.best_iteration + 1),
        "params": params,
        "test_auc": round(auc, 4),
        "test_gini": round(2 * auc - 1, 4),
        "test_brier": round(float(brier_score_loss(y_test, p_test)), 4),
        "bands_on_test": bands,
        "calibration_by_decile": calibration,
        "feature_importance_gain": importance,
        "features": FEATURE_COLS,
        "trained_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    print(json.dumps({k: metrics[k] for k in ("test_auc", "test_gini", "test_brier", "trees",
                                              "bands_on_test", "calibration_by_decile")}, indent=2))

    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    joblib.dump({"model": model, "features": FEATURE_COLS, "auc": auc, "metrics": metrics}, MODEL_PATH)
    METRICS_PATH.write_text(json.dumps(metrics, indent=2) + "\n")
    print(f"Saved {MODEL_PATH} and {METRICS_PATH}", flush=True)
    return metrics


# ── Scoring ────────────────────────────────────────────────────────────────


def load_warning_score_model():
    """Load trained model from disk -> (model, features)."""
    data = joblib.load(MODEL_PATH)
    if data.get("features") != FEATURE_COLS:
        raise FileNotFoundError("Saved warning-score model predates the current feature set; retrain it")
    return data["model"], data["features"]


def predict_warning_scores(features: pd.DataFrame, model=None) -> Dict[str, WarningScoreResult]:
    """Score many customers. `features` is indexed by customer_id with FEATURE_COLS."""
    if model is None:
        model, _ = load_warning_score_model()
    if features.empty:
        return {}
    import xgboost as xgb

    X = features[FEATURE_COLS].astype(float)
    probs = model.predict_proba(X)[:, 1]
    contribs = model.get_booster().predict(xgb.DMatrix(X, feature_names=FEATURE_COLS), pred_contribs=True)

    results = {}
    for i, cid in enumerate(X.index):
        p = float(probs[i])
        lift = p / BASELINE_DEFAULT_RATE
        score = float(lift_to_score(np.array([lift]))[0])
        row = contribs[i][:-1]  # last column is the bias term
        order = np.argsort(-np.abs(row))[:5]
        top = [
            {"feature": FEATURE_COLS[j], "label": FEATURE_LABELS[FEATURE_COLS[j]],
             "value": None if pd.isna(X.iat[i, j]) else round(float(X.iat[i, j]), 4),
             "contribution": round(float(row[j]), 4)}
            for j in order
        ]
        raises = [t["label"] for t in top if t["contribution"] > 0.05][:3]
        lowers = [t["label"] for t in top if t["contribution"] < -0.05][:2]
        indicators = [f"Model: predicted default probability {p:.1%} ({lift:.2f}x the 8.07% baseline)"]
        if raises:
            indicators.append("Model: risk raised by " + ", ".join(raises))
        if lowers:
            indicators.append("Model: risk lowered by " + ", ".join(lowers))
        results[str(cid)] = WarningScoreResult(
            customer_id=str(cid), warning_score=round(score, 2), risk_category=score_to_category(score),
            default_probability=round(p, 4), lift=round(lift, 3), top_contributors=top,
            rule_scores={}, indicators=indicators,
        )
    return results


def predict_warning_score(feature_dict: dict, model=None) -> WarningScoreResult:
    """Single-customer wrapper (used by core/ews_engine.py). Missing keys = unknown."""
    cid = str(feature_dict.get("customer_id", "unknown"))
    X = pd.DataFrame([{f: feature_dict.get(f, np.nan) for f in FEATURE_COLS}], index=[cid])
    return predict_warning_scores(X, model)[cid]


if __name__ == "__main__":
    train_warning_score_model(os.path.join(os.path.dirname(__file__), "..", "data", "raw"))
