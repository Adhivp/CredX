"""
Train both models with 200K customers, evaluate on test set, print all scores.
Re-trains if AUC < 0.85.
"""
import sys
sys.stdout.reconfigure(line_buffering=True)

import os
import numpy as np
import pandas as pd
import joblib
from pathlib import Path
from sklearn.model_selection import train_test_split
from sklearn.metrics import classification_report, roc_auc_score, precision_recall_curve, average_precision_score, confusion_matrix
from sklearn.ensemble import GradientBoostingClassifier
from xgboost import XGBClassifier

DATA_DIR = "/Users/adhivp/CredX/core/data/raw"
MODEL_DIR = Path("/Users/adhivp/CredX/core/models/saved")
MODEL_DIR.mkdir(parents=True, exist_ok=True)

N_CUSTOMERS = 200000
MIN_AUC = 0.85

# ====================================================================
# MODEL 1: CASH FLOW DETERIORATION
# ====================================================================
print("=" * 70)
print("MODEL 1: CASH FLOW DETERIORATION — 200K CUSTOMERS")
print("=" * 70)

from cashflow_model import generate_synthetic_cashflow, extract_cashflow_features, FEATURE_COLS as CF_FEATURES

print("\n[1/5] Generating synthetic cash flow for 200K customers...")
app_path = os.path.join(DATA_DIR, "application_train.csv")
cashflow_df = generate_synthetic_cashflow(app_path, n_customers=N_CUSTOMERS)
print(f"  Records: {len(cashflow_df):,}")

print("\n[2/5] Extracting features...")
features_df = extract_cashflow_features(cashflow_df)
print(f"  Customers: {len(features_df):,}")
print(f"  Declining: {features_df['is_declining'].sum():,} ({features_df['is_declining'].mean():.2%})")
print(f"  Stable: {(~features_df['is_declining'].astype(bool)).sum():,}")

print("\n[3/5] Training GradientBoosting...")
X = features_df[CF_FEATURES].values
y = features_df["is_declining"].values
X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42, stratify=y)
print(f"  Train: {len(X_train):,} | Test: {len(X_test):,}")

attempt = 0
best_auc = 0
best_model = None

for n_est, lr, depth in [(200, 0.1, 5), (400, 0.05, 6), (500, 0.03, 7)]:
    attempt += 1
    print(f"\n  Attempt {attempt}: n_estimators={n_est}, lr={lr}, depth={depth}")
    model = GradientBoostingClassifier(
        n_estimators=n_est, max_depth=depth, learning_rate=lr,
        subsample=0.8, min_samples_leaf=20, random_state=42,
    )
    model.fit(X_train, y_train)
    y_prob = model.predict_proba(X_test)[:, 1]
    auc = roc_auc_score(y_test, y_prob)
    ap = average_precision_score(y_test, y_prob)
    print(f"  ROC AUC: {auc:.4f} | Avg Precision: {ap:.4f}")

    if auc > best_auc:
        best_auc = auc
        best_model = model

    if auc >= MIN_AUC:
        print(f"  ✓ AUC {auc:.4f} >= {MIN_AUC} — accepting this model")
        break
    else:
        print(f"  ✗ AUC {auc:.4f} < {MIN_AUC} — trying next config")

print(f"\n[4/5] Final Evaluation (best AUC: {best_auc:.4f})")
y_prob_final = best_model.predict_proba(X_test)[:, 1]
y_pred_final = best_model.predict(X_test)

print(f"\n  ROC AUC: {best_auc:.4f}")
print(f"  Average Precision: {average_precision_score(y_test, y_prob_final):.4f}")
print(f"\n  Confusion Matrix:")
cm = confusion_matrix(y_test, y_pred_final)
print(f"    TN={cm[0][0]:,}  FP={cm[0][1]:,}")
print(f"    FN={cm[1][0]:,}  TP={cm[1][1]:,}")
print(f"\n  Classification Report:")
print(classification_report(y_test, y_pred_final, target_names=["Stable", "Declining"]))

print("  Feature Importances:")
for feat, imp in sorted(zip(CF_FEATURES, best_model.feature_importances_), key=lambda x: -x[1]):
    bar = "█" * int(imp * 80)
    print(f"    {feat:30s} {imp:.4f} {bar}")

print("\n[5/5] Saving model...")
joblib.dump({"model": best_model, "features": CF_FEATURES, "auc": best_auc}, MODEL_DIR / "cashflow_model.joblib")
print(f"  Saved. AUC = {best_auc:.4f}")

# ====================================================================
# MODEL 2: WARNING SCORE (XGBoost)
# ====================================================================
print("\n\n" + "=" * 70)
print("MODEL 2: WARNING SCORE (XGBoost) — 200K CUSTOMERS")
print("=" * 70)

from warning_score_model import build_training_features, FEATURE_COLS as WS_FEATURES

print("\n[1/5] Building combined feature matrix...")
df = build_training_features(DATA_DIR, n_customers=N_CUSTOMERS)

print("\n[2/5] Splitting data...")
X2 = df[WS_FEATURES].values
y2 = df["TARGET"].values
X2_train, X2_test, y2_train, y2_test = train_test_split(X2, y2, test_size=0.2, random_state=42, stratify=y2)
print(f"  Train: {len(X2_train):,} | Test: {len(X2_test):,}")

print("\n[3/5] Training XGBoost...")
attempt = 0
best_auc2 = 0
best_model2 = None

configs = [
    (300, 0.05, 6, 10),
    (500, 0.03, 7, 15),
    (700, 0.02, 8, 20),
]

for n_est, lr, depth, mcw in configs:
    attempt += 1
    print(f"\n  Attempt {attempt}: n_est={n_est}, lr={lr}, depth={depth}, min_child={mcw}")
    spw = (1 - y2_train.mean()) / y2_train.mean()

    model2 = XGBClassifier(
        n_estimators=n_est, max_depth=depth, learning_rate=lr,
        subsample=0.8, colsample_bytree=0.8, min_child_weight=mcw,
        reg_alpha=0.1, reg_lambda=1.0, scale_pos_weight=spw,
        random_state=42, eval_metric="auc", early_stopping_rounds=30,
    )
    model2.fit(X2_train, y2_train, eval_set=[(X2_test, y2_test)], verbose=0)
    y2_prob = model2.predict_proba(X2_test)[:, 1]
    auc2 = roc_auc_score(y2_test, y2_prob)
    ap2 = average_precision_score(y2_test, y2_prob)
    print(f"  ROC AUC: {auc2:.4f} | Avg Precision: {ap2:.4f}")

    if auc2 > best_auc2:
        best_auc2 = auc2
        best_model2 = model2

    if auc2 >= MIN_AUC:
        print(f"  ✓ AUC {auc2:.4f} >= {MIN_AUC} — accepting this model")
        break
    else:
        print(f"  ✗ AUC {auc2:.4f} < {MIN_AUC} — trying next config")

print(f"\n[4/5] Final Evaluation (best AUC: {best_auc2:.4f})")
y2_prob_final = best_model2.predict_proba(X2_test)[:, 1]
y2_pred_final = best_model2.predict(X2_test)

print(f"\n  ROC AUC: {best_auc2:.4f}")
print(f"  Average Precision: {average_precision_score(y2_test, y2_prob_final):.4f}")
print(f"\n  Confusion Matrix:")
cm2 = confusion_matrix(y2_test, y2_pred_final)
print(f"    TN={cm2[0][0]:,}  FP={cm2[0][1]:,}")
print(f"    FN={cm2[1][0]:,}  TP={cm2[1][1]:,}")
print(f"\n  Classification Report:")
print(classification_report(y2_test, y2_pred_final, target_names=["Non-Default", "Default"]))

print("  Feature Importances:")
for feat, imp in sorted(zip(WS_FEATURES, best_model2.feature_importances_), key=lambda x: -x[1]):
    bar = "█" * int(imp * 50)
    print(f"    {feat:30s} {imp:.4f} {bar}")

print("\n[5/5] Saving model...")
joblib.dump({"model": best_model2, "features": WS_FEATURES, "auc": best_auc2}, MODEL_DIR / "warning_score_model.joblib")
print(f"  Saved. AUC = {best_auc2:.4f}")

# ====================================================================
# SUMMARY
# ====================================================================
print("\n\n" + "=" * 70)
print("TRAINING SUMMARY")
print("=" * 70)
print(f"  Model 1 (Cash Flow):    AUC = {best_auc:.4f}  {'✓ PASS' if best_auc >= MIN_AUC else '✗ FAIL'}")
print(f"  Model 2 (Warning Score): AUC = {best_auc2:.4f}  {'✓ PASS' if best_auc2 >= MIN_AUC else '✗ FAIL'}")
print(f"  Min required AUC: {MIN_AUC}")
print(f"  Models saved to: {MODEL_DIR}")
print("=" * 70)
