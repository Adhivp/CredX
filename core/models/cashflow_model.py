"""
Model 1: Cash Flow Deterioration Detection
Trained on synthetic cash flow data generated from Home Credit customer profiles.
Uses gradient boosted trees on rolling cash flow features to predict sustained decline.
"""

import pandas as pd
import numpy as np
import joblib
import os
from pathlib import Path
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.model_selection import train_test_split
from sklearn.metrics import classification_report, roc_auc_score
from dataclasses import dataclass
from typing import Dict, Optional

MODEL_DIR = Path(__file__).parent / "saved"
MODEL_PATH = MODEL_DIR / "cashflow_model.joblib"

FEATURE_COLS = [
    "slope_3m", "slope_6m",
    "volatility_3m", "volatility_6m",
    "pct_change_3m", "pct_change_6m",
    "min_to_mean_ratio",
    "declining_months_ratio",
    "current_vs_peak",
    "avg_inflow_last3m", "avg_inflow_last6m",
    "inflow_trend_consistency",
]


@dataclass
class CashFlowSignal:
    customer_id: str
    deterioration_probability: float  # 0-1
    score: float  # 0-100
    slope_3m: float
    slope_6m: float
    pct_change_3m: float
    pct_change_6m: float
    volatility: float
    declining_months: int
    total_months: int
    indicators: list


def generate_synthetic_cashflow(app_path: str, n_customers: int = 200000, seed: int = 42) -> pd.DataFrame:
    """
    Generate synthetic monthly cash flow data for Home Credit customers.
    Patterns are derived from their actual repayment behaviour and default status.
    """
    np.random.seed(seed)
    app = pd.read_csv(app_path, usecols=["SK_ID_CURR", "TARGET", "AMT_INCOME_TOTAL", "AMT_ANNUITY"])
    app = app.dropna(subset=["AMT_INCOME_TOTAL"]).sample(min(n_customers, len(app)), random_state=seed)

    records = []
    for _, row in app.iterrows():
        cid = row["SK_ID_CURR"]
        monthly_income = row["AMT_INCOME_TOTAL"] / 12
        is_default = row["TARGET"]
        months = 12

        if is_default:
            # 70% chance of declining pattern, 30% volatile
            if np.random.random() < 0.7:
                trend = np.linspace(0, -np.random.uniform(0.15, 0.45), months)
            else:
                trend = np.linspace(0, -np.random.uniform(0.05, 0.15), months)
        else:
            # 80% stable, 15% slight growth, 5% slight decline
            r = np.random.random()
            if r < 0.80:
                trend = np.linspace(0, np.random.uniform(-0.05, 0.05), months)
            elif r < 0.95:
                trend = np.linspace(0, np.random.uniform(0.05, 0.15), months)
            else:
                trend = np.linspace(0, -np.random.uniform(0.05, 0.15), months)

        noise_level = np.random.uniform(0.05, 0.20)
        noise = np.random.normal(0, noise_level, months)

        # Seasonal component for some customers
        if np.random.random() < 0.3:
            seasonal = 0.08 * np.sin(np.linspace(0, 2 * np.pi, months))
        else:
            seasonal = np.zeros(months)

        inflows = monthly_income * (1 + trend + noise + seasonal)
        inflows = np.maximum(inflows, monthly_income * 0.05)  # floor at 5% of base

        for m in range(months):
            records.append({
                "customer_id": cid,
                "month": m + 1,
                "monthly_inflow": round(float(inflows[m]), 2),
                "is_declining": int(is_default),
            })

    return pd.DataFrame(records)


def extract_cashflow_features(cashflow_df: pd.DataFrame) -> pd.DataFrame:
    """Extract rolling features from monthly cash flow time series."""
    features = []

    for cid, group in cashflow_df.groupby("customer_id"):
        group = group.sort_values("month")
        inflows = group["monthly_inflow"].values
        n = len(inflows)
        # Training label; absent when scoring real customers.
        is_declining = group["is_declining"].iloc[0] if "is_declining" in group else 0

        if n < 3:
            continue

        months_arr = np.arange(n)

        # Linear slope over 3m and 6m (normalised by mean)
        mean_inflow = np.mean(inflows) if np.mean(inflows) > 0 else 1
        last3 = inflows[-3:]
        slope_3m = np.polyfit(np.arange(3), last3 / mean_inflow, 1)[0]
        slope_6m = np.polyfit(np.arange(min(6, n)), inflows[-min(6, n):] / mean_inflow, 1)[0]

        # Volatility (coefficient of variation)
        volatility_3m = np.std(last3) / mean_inflow if mean_inflow > 0 else 0
        volatility_6m = np.std(inflows[-min(6, n):]) / mean_inflow if mean_inflow > 0 else 0

        # Percentage change
        avg_first3 = np.mean(inflows[:3])
        avg_last3 = np.mean(last3)
        pct_change_3m = (avg_last3 - avg_first3) / avg_first3 if avg_first3 > 0 else 0

        avg_first6 = np.mean(inflows[:min(6, n)])
        avg_last6 = np.mean(inflows[-min(6, n):])
        pct_change_6m = (avg_last6 - avg_first6) / avg_first6 if avg_first6 > 0 else 0

        # Min-to-mean ratio
        min_to_mean_ratio = np.min(inflows) / mean_inflow if mean_inflow > 0 else 0

        # Declining months ratio
        month_changes = np.diff(inflows)
        declining_months_ratio = np.sum(month_changes < 0) / len(month_changes) if len(month_changes) > 0 else 0

        # Current vs peak
        current_vs_peak = inflows[-1] / np.max(inflows) if np.max(inflows) > 0 else 0

        # Average inflows
        avg_inflow_last3m = np.mean(last3)
        avg_inflow_last6m = np.mean(inflows[-min(6, n):])

        # Trend consistency (how many consecutive months declining at end)
        consecutive_decline = 0
        for i in range(len(month_changes) - 1, -1, -1):
            if month_changes[i] < 0:
                consecutive_decline += 1
            else:
                break
        inflow_trend_consistency = consecutive_decline / n

        features.append({
            "customer_id": cid,
            "slope_3m": slope_3m,
            "slope_6m": slope_6m,
            "volatility_3m": volatility_3m,
            "volatility_6m": volatility_6m,
            "pct_change_3m": pct_change_3m,
            "pct_change_6m": pct_change_6m,
            "min_to_mean_ratio": min_to_mean_ratio,
            "declining_months_ratio": declining_months_ratio,
            "current_vs_peak": current_vs_peak,
            "avg_inflow_last3m": avg_inflow_last3m,
            "avg_inflow_last6m": avg_inflow_last6m,
            "inflow_trend_consistency": inflow_trend_consistency,
            "is_declining": is_declining,
        })

    return pd.DataFrame(features)


def train_cashflow_model(app_path: str, n_customers: int = 200000) -> dict:
    """Full training pipeline: generate data, extract features, train, save."""
    print("=" * 60)
    print("CASH FLOW DETERIORATION MODEL — TRAINING")
    print("=" * 60)

    print("\n[1/4] Generating synthetic cash flow data...", flush=True)
    cashflow_df = generate_synthetic_cashflow(app_path, n_customers)
    print(f"  Generated {len(cashflow_df):,} monthly records for {cashflow_df['customer_id'].nunique():,} customers", flush=True)

    print("\n[2/4] Extracting features...", flush=True)
    features_df = extract_cashflow_features(cashflow_df)
    print(f"  Extracted {len(FEATURE_COLS)} features for {len(features_df):,} customers", flush=True)
    print(f"  Declining: {features_df['is_declining'].sum():,} ({features_df['is_declining'].mean():.2%})", flush=True)

    print("\n[3/4] Training model...", flush=True)
    X = features_df[FEATURE_COLS].values
    y = features_df["is_declining"].values

    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42, stratify=y)
    print(f"  Train: {len(X_train):,} | Test: {len(X_test):,}", flush=True)

    model = GradientBoostingClassifier(
        n_estimators=200,
        max_depth=5,
        learning_rate=0.1,
        subsample=0.8,
        min_samples_leaf=20,
        random_state=42,
    )
    model.fit(X_train, y_train)

    y_pred = model.predict(X_test)
    y_prob = model.predict_proba(X_test)[:, 1]
    auc = roc_auc_score(y_test, y_prob)

    print(f"\n  ROC AUC: {auc:.4f}", flush=True)
    print("\n  Classification Report:", flush=True)
    print(classification_report(y_test, y_pred, target_names=["Stable", "Declining"]), flush=True)

    print("\n  Feature Importances:", flush=True)
    importances = sorted(zip(FEATURE_COLS, model.feature_importances_), key=lambda x: -x[1])
    for feat, imp in importances:
        bar = "█" * int(imp * 100)
        print(f"    {feat:30s} {imp:.4f} {bar}", flush=True)

    print("\n[4/4] Saving model...", flush=True)
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    joblib.dump({"model": model, "features": FEATURE_COLS, "auc": auc}, MODEL_PATH)
    print(f"  Saved to {MODEL_PATH}", flush=True)
    import json, time
    (MODEL_DIR / "cashflow_model.metrics.json").write_text(json.dumps({
        "dataset": "Synthetic 12-month cash flows generated from Home Credit application_train "
                   "(income, annuity, TARGET)",
        "customers": int(features_df["customer_id"].nunique()),
        "split_train_test": [int(len(X_train)), int(len(X_test))],
        "declining_share": round(float(features_df["is_declining"].mean()), 4),
        "algorithm": "scikit-learn GradientBoostingClassifier",
        "params": {"n_estimators": 200, "max_depth": 5, "learning_rate": 0.1,
                   "subsample": 0.8, "min_samples_leaf": 20},
        "test_auc": round(float(auc), 4),
        "feature_importance": [{"feature": f, "importance": round(float(i), 4)} for f, i in importances],
        "trained_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }, indent=2) + "\n")

    print("\n" + "=" * 60)
    print(f"TRAINING COMPLETE — AUC: {auc:.4f}")
    print("=" * 60, flush=True)

    return {"auc": auc, "model": model, "features": FEATURE_COLS}


def load_cashflow_model():
    """Load trained model from disk."""
    data = joblib.load(MODEL_PATH)
    return data["model"], data["features"]


def predict_cashflow_deterioration(
    cashflow_df: pd.DataFrame,
    model=None,
) -> Dict[str, CashFlowSignal]:
    """
    Predict cash flow deterioration for customers.

    cashflow_df columns:
        - customer_id: str
        - month: int (sequential month number)
        - monthly_inflow: float

    Returns dict of customer_id -> CashFlowSignal
    """
    if model is None:
        model, _ = load_cashflow_model()

    features_df = extract_cashflow_features(cashflow_df)
    if features_df.empty:
        return {}

    X = features_df[FEATURE_COLS].values
    probs = model.predict_proba(X)[:, 1]

    results = {}
    for i, (_, row) in enumerate(features_df.iterrows()):
        cid = str(row["customer_id"])
        prob = float(probs[i])
        score = round(prob * 100, 2)

        indicators = []
        if row["slope_6m"] < -0.02:
            indicators.append(f"Cash flow declining: 6-month slope = {row['slope_6m']:.3f}")
        if row["pct_change_6m"] < -0.10:
            indicators.append(f"Income dropped {abs(row['pct_change_6m']):.0%} over 6 months")
        if row["declining_months_ratio"] > 0.5:
            indicators.append(f"{row['declining_months_ratio']:.0%} of months show declining inflows")
        if row["current_vs_peak"] < 0.7:
            indicators.append(f"Current inflow at {row['current_vs_peak']:.0%} of peak")
        if row["volatility_6m"] > 0.25:
            indicators.append(f"High cash flow volatility: {row['volatility_6m']:.2f}")

        declining_months = int(row["declining_months_ratio"] * 11)

        results[cid] = CashFlowSignal(
            customer_id=cid,
            deterioration_probability=round(prob, 4),
            score=score,
            slope_3m=round(float(row["slope_3m"]), 4),
            slope_6m=round(float(row["slope_6m"]), 4),
            pct_change_3m=round(float(row["pct_change_3m"]), 4),
            pct_change_6m=round(float(row["pct_change_6m"]), 4),
            volatility=round(float(row["volatility_6m"]), 4),
            declining_months=declining_months,
            total_months=12,
            indicators=indicators,
        )

    return results


if __name__ == "__main__":
    APP_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "raw", "application_train.csv")
    train_cashflow_model(APP_PATH, n_customers=307511)  # every Home Credit applicant
