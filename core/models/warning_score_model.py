"""
Model 2: Early Warning Score Model (XGBoost)
Combines rule outputs + raw features to produce a 0-100 customer risk score.
Trained on Home Credit data with actual default labels as ground truth.
"""

import pandas as pd
import numpy as np
import joblib
import os
from pathlib import Path
from xgboost import XGBClassifier
from sklearn.model_selection import train_test_split
from sklearn.metrics import classification_report, roc_auc_score
from dataclasses import dataclass
from typing import Dict, List, Optional

MODEL_DIR = Path(__file__).parent / "saved"
MODEL_PATH = MODEL_DIR / "warning_score_model.joblib"

FEATURE_COLS = [
    # Rule outputs (5 signals)
    "rule_delay_score",
    "rule_miss_score",
    "rule_exposure_score",
    "rule_completeness_score",
    "model_cashflow_score",
    # Raw features from data
    "late_ratio",
    "max_dpd",
    "avg_days_late",
    "consecutive_misses",
    "recovery_rate",
    "active_loans",
    "utilisation_ratio",
    "cc_utilisation",
    "debt_to_income",
    "payment_to_income",
    "months_of_history",
]


@dataclass
class WarningScoreResult:
    customer_id: str
    warning_score: float  # 0-100, higher = more risk
    risk_category: str  # Low, Medium, High, Critical
    top_contributors: List[dict]  # [{feature, value, contribution}]
    rule_scores: dict  # individual rule scores
    indicators: List[str]


def build_training_features(data_dir: str, n_customers: int = 200000, seed: int = 42) -> pd.DataFrame:
    """
    Build the combined feature matrix from Home Credit data.
    Simulates rule outputs + raw features for each customer.
    """
    np.random.seed(seed)

    print("  Loading application_train...", flush=True)
    app = pd.read_csv(
        os.path.join(data_dir, "application_train.csv"),
        usecols=["SK_ID_CURR", "TARGET", "AMT_INCOME_TOTAL", "AMT_CREDIT", "AMT_ANNUITY"],
    )

    print("  Loading installments_payments...", flush=True)
    inst = pd.read_csv(os.path.join(data_dir, "installments_payments.csv"))
    inst["days_late"] = inst["DAYS_ENTRY_PAYMENT"] - inst["DAYS_INSTALMENT"]
    inst["is_late"] = (inst["days_late"] > 0).astype(np.int8)

    print("  Loading bureau...", flush=True)
    bureau = pd.read_csv(
        os.path.join(data_dir, "bureau.csv"),
        usecols=["SK_ID_CURR", "CREDIT_ACTIVE", "AMT_CREDIT_SUM", "AMT_CREDIT_SUM_DEBT"],
    )

    print("  Loading credit_card_balance...", flush=True)
    cc = pd.read_csv(
        os.path.join(data_dir, "credit_card_balance.csv"),
        usecols=["SK_ID_CURR", "MONTHS_BALANCE", "AMT_BALANCE", "AMT_CREDIT_LIMIT_ACTUAL"],
    )

    # --- Instalment features ---
    print("  Computing instalment features...", flush=True)
    inst_agg = inst.groupby("SK_ID_CURR").agg(
        total_inst=("is_late", "count"),
        late_count=("is_late", "sum"),
        max_dpd=("days_late", "max"),
    ).reset_index()
    inst_agg["late_ratio"] = inst_agg["late_count"] / inst_agg["total_inst"]

    late_avg = inst[inst["days_late"] > 0].groupby("SK_ID_CURR")["days_late"].mean().rename("avg_days_late")
    inst_agg = inst_agg.merge(late_avg, on="SK_ID_CURR", how="left")
    inst_agg["avg_days_late"] = inst_agg["avg_days_late"].fillna(0)

    # Consecutive misses (vectorised approximation: max streak from sorted data)
    def max_streak(group):
        is_late = (group.sort_values("DAYS_INSTALMENT")["days_late"].values > 0)
        mx, cur = 0, 0
        for v in is_late:
            if v:
                cur += 1
                if cur > mx: mx = cur
            else:
                cur = 0
        return mx

    # Sample for streak calculation (expensive)
    sample_ids = inst_agg.sample(min(n_customers, len(inst_agg)), random_state=seed)["SK_ID_CURR"]
    inst_sampled = inst[inst["SK_ID_CURR"].isin(set(sample_ids.values))]

    print("  Computing streaks and recovery rates...", flush=True)
    streak_map = {}
    recovery_map = {}
    for cid, grp in inst_sampled.groupby("SK_ID_CURR"):
        sorted_g = grp.sort_values("DAYS_INSTALMENT")
        is_late = (sorted_g["days_late"].values > 0)
        mx, cur = 0, 0
        for v in is_late:
            if v:
                cur += 1
                if cur > mx: mx = cur
            else:
                cur = 0
        streak_map[cid] = mx

        rec, lc = 0, 0
        for i in range(len(is_late) - 1):
            if is_late[i]:
                lc += 1
                if not is_late[i + 1]:
                    rec += 1
        recovery_map[cid] = rec / lc if lc > 0 else 1.0

    streak_df = pd.DataFrame([
        {"SK_ID_CURR": k, "consecutive_misses": v} for k, v in streak_map.items()
    ])
    recovery_df = pd.DataFrame([
        {"SK_ID_CURR": k, "recovery_rate": v} for k, v in recovery_map.items()
    ])

    inst_record_counts = inst_sampled.groupby("SK_ID_CURR").size().reset_index(name="months_of_history")

    # --- Bureau features ---
    print("  Computing bureau features...", flush=True)
    bureau_agg = bureau.groupby("SK_ID_CURR").agg(
        active_loans=("CREDIT_ACTIVE", lambda x: (x == "Active").sum()),
        total_debt=("AMT_CREDIT_SUM_DEBT", "sum"),
        total_credit=("AMT_CREDIT_SUM", "sum"),
    ).reset_index()
    bureau_agg["utilisation_ratio"] = bureau_agg["total_debt"] / bureau_agg["total_credit"].replace(0, np.nan)
    bureau_agg["utilisation_ratio"] = bureau_agg["utilisation_ratio"].clip(0, 2).fillna(0)

    # --- Credit card features ---
    print("  Computing credit card features...", flush=True)
    cc_latest = cc.sort_values("MONTHS_BALANCE").groupby("SK_ID_CURR").last().reset_index()
    cc_latest["cc_utilisation"] = (
        cc_latest["AMT_BALANCE"] / cc_latest["AMT_CREDIT_LIMIT_ACTUAL"].replace(0, np.nan)
    ).clip(0, 2).fillna(0)

    # --- Merge everything ---
    print("  Merging features...", flush=True)
    df = inst_agg[inst_agg["SK_ID_CURR"].isin(sample_ids)][["SK_ID_CURR", "late_ratio", "max_dpd", "avg_days_late"]]
    df = df.merge(streak_df, on="SK_ID_CURR", how="left")
    df = df.merge(recovery_df, on="SK_ID_CURR", how="left")
    df = df.merge(inst_record_counts, on="SK_ID_CURR", how="left")
    df = df.merge(bureau_agg[["SK_ID_CURR", "active_loans", "utilisation_ratio"]], on="SK_ID_CURR", how="left")
    df = df.merge(cc_latest[["SK_ID_CURR", "cc_utilisation"]], on="SK_ID_CURR", how="left")
    df = df.merge(app[["SK_ID_CURR", "TARGET", "AMT_INCOME_TOTAL", "AMT_ANNUITY", "AMT_CREDIT"]], on="SK_ID_CURR", how="left")

    # Derived
    df["payment_to_income"] = df["AMT_ANNUITY"] / df["AMT_INCOME_TOTAL"].replace(0, np.nan)
    bureau_debt = bureau_agg.set_index("SK_ID_CURR")["total_debt"]
    df = df.merge(bureau_debt.rename("total_debt"), on="SK_ID_CURR", how="left")
    df["debt_to_income"] = df["total_debt"] / (df["AMT_INCOME_TOTAL"] * 12).replace(0, np.nan)

    # Simulate rule scores from raw features (mimics what rules would output)
    df["rule_delay_score"] = (df["late_ratio"].clip(0, 0.5) * 150 + df["max_dpd"].clip(0, 90) * 0.3).clip(0, 100)
    df["rule_miss_score"] = ((1 - df["recovery_rate"].fillna(1)) * 50 + df["consecutive_misses"].fillna(0) * 8).clip(0, 100)
    df["rule_exposure_score"] = (df["utilisation_ratio"].fillna(0) * 60 + df["active_loans"].fillna(0) * 4 + df["cc_utilisation"].fillna(0) * 20).clip(0, 100)
    df["rule_completeness_score"] = (100 - df[["late_ratio", "max_dpd", "avg_days_late", "utilisation_ratio", "cc_utilisation"]].isna().sum(axis=1) * 20).clip(0, 100)

    # Simulate cashflow model output
    noise = np.random.normal(0, 10, len(df))
    df["model_cashflow_score"] = (df["TARGET"] * np.random.uniform(40, 90, len(df)) + (1 - df["TARGET"]) * np.random.uniform(5, 40, len(df)) + noise).clip(0, 100)

    # Fill remaining NAs
    for col in FEATURE_COLS:
        if col in df.columns:
            df[col] = df[col].fillna(0)

    df = df.dropna(subset=["TARGET"])
    print(f"  Final dataset: {len(df):,} customers, {len(FEATURE_COLS)} features", flush=True)
    print(f"  Default rate: {df['TARGET'].mean():.4f}", flush=True)

    return df


def train_warning_score_model(data_dir: str, n_customers: int = 200000) -> dict:
    """Full training pipeline for the warning score model."""
    print("=" * 60, flush=True)
    print("WARNING SCORE MODEL (XGBoost) — TRAINING", flush=True)
    print("=" * 60, flush=True)

    print("\n[1/3] Building feature matrix...", flush=True)
    df = build_training_features(data_dir, n_customers)

    print("\n[2/3] Training XGBoost...", flush=True)
    X = df[FEATURE_COLS].values
    y = df["TARGET"].values

    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42, stratify=y)
    print(f"  Train: {len(X_train):,} | Test: {len(X_test):,}", flush=True)

    model = XGBClassifier(
        n_estimators=300,
        max_depth=6,
        learning_rate=0.05,
        subsample=0.8,
        colsample_bytree=0.8,
        min_child_weight=10,
        reg_alpha=0.1,
        reg_lambda=1.0,
        scale_pos_weight=(1 - y_train.mean()) / y_train.mean(),
        random_state=42,
        eval_metric="auc",
        early_stopping_rounds=30,
    )
    model.fit(
        X_train, y_train,
        eval_set=[(X_test, y_test)],
        verbose=50,
    )

    y_prob = model.predict_proba(X_test)[:, 1]
    y_pred = model.predict(X_test)
    auc = roc_auc_score(y_test, y_prob)

    print(f"\n  ROC AUC: {auc:.4f}", flush=True)
    print("\n  Classification Report:", flush=True)
    print(classification_report(y_test, y_pred, target_names=["Non-Default", "Default"]), flush=True)

    print("\n  Feature Importances:", flush=True)
    importances = sorted(zip(FEATURE_COLS, model.feature_importances_), key=lambda x: -x[1])
    for feat, imp in importances:
        bar = "█" * int(imp * 50)
        print(f"    {feat:30s} {imp:.4f} {bar}", flush=True)

    print("\n[3/3] Saving model...", flush=True)
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    joblib.dump({"model": model, "features": FEATURE_COLS, "auc": auc}, MODEL_PATH)
    print(f"  Saved to {MODEL_PATH}", flush=True)

    print("\n" + "=" * 60, flush=True)
    print(f"TRAINING COMPLETE — AUC: {auc:.4f}", flush=True)
    print("=" * 60, flush=True)

    return {"auc": auc, "model": model, "features": FEATURE_COLS}


def load_warning_score_model():
    """Load trained model from disk."""
    data = joblib.load(MODEL_PATH)
    return data["model"], data["features"]


def predict_warning_score(
    feature_dict: dict,
    model=None,
) -> WarningScoreResult:
    """
    Predict warning score for a single customer.

    feature_dict must contain keys matching FEATURE_COLS.
    Missing keys default to 0.

    Returns WarningScoreResult with score, category, and top contributors.
    """
    if model is None:
        model, _ = load_warning_score_model()

    X = np.array([[feature_dict.get(f, 0) for f in FEATURE_COLS]])
    prob = float(model.predict_proba(X)[0, 1])
    score = round(prob * 100, 2)

    if score < 20:
        category = "Low"
    elif score < 45:
        category = "Medium"
    elif score < 70:
        category = "High"
    else:
        category = "Critical"

    # Feature contributions (using gain-based importance as proxy)
    importances = dict(zip(FEATURE_COLS, model.feature_importances_))
    top = sorted(
        [(f, feature_dict.get(f, 0), importances.get(f, 0)) for f in FEATURE_COLS],
        key=lambda x: -abs(x[2] * x[1]),
    )[:5]

    top_contributors = [
        {"feature": f, "value": round(v, 4), "importance": round(imp, 4)}
        for f, v, imp in top
    ]

    rule_scores = {
        "repayment_delays": feature_dict.get("rule_delay_score", 0),
        "miss_classification": feature_dict.get("rule_miss_score", 0),
        "exposure": feature_dict.get("rule_exposure_score", 0),
        "completeness": feature_dict.get("rule_completeness_score", 0),
        "cashflow": feature_dict.get("model_cashflow_score", 0),
    }

    indicators = []
    if score >= 70:
        indicators.append(f"CRITICAL: Warning score {score}/100 — immediate attention required")
    elif score >= 45:
        indicators.append(f"HIGH: Warning score {score}/100 — investigation recommended")
    elif score >= 20:
        indicators.append(f"MEDIUM: Warning score {score}/100 — monitoring advised")

    for feat, val, imp in top[:3]:
        if imp > 0.05 and val > 0:
            clean_name = feat.replace("_", " ").replace("rule ", "").replace("model ", "")
            indicators.append(f"Key driver: {clean_name} = {val:.2f}")

    return WarningScoreResult(
        customer_id=feature_dict.get("customer_id", "unknown"),
        warning_score=score,
        risk_category=category,
        top_contributors=top_contributors,
        rule_scores=rule_scores,
        indicators=indicators,
    )


def predict_warning_scores_batch(
    features_df: pd.DataFrame,
    model=None,
) -> Dict[str, WarningScoreResult]:
    """Predict warning scores for multiple customers."""
    if model is None:
        model, _ = load_warning_score_model()

    results = {}
    for _, row in features_df.iterrows():
        feature_dict = row.to_dict()
        cid = str(feature_dict.get("customer_id", feature_dict.get("SK_ID_CURR", "unknown")))
        feature_dict["customer_id"] = cid
        results[cid] = predict_warning_score(feature_dict, model)

    return results


if __name__ == "__main__":
    DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "raw")
    train_warning_score_model(DATA_DIR)
