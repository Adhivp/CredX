"""Model 3 (Level 2): probability of deterioration, trained on Home Credit history.

Point-in-time panel: for each customer a cutoff T = last due date - 180 days.
Features use only instalments / card balances known at T (levels + 3-month changes);
label = an instalment more than 30 days past due in (T, T+180] (RBI SMA-1 entry).
Also learned from the same data (no hand-set thresholds):
  - trend cut points on the 3-month change in log-odds (shallow decision tree),
  - credit conversion factor (CCF) of undrawn card limits before delinquency.
Sampled for speed (see N_* below). Train: python -m core.models.deterioration_model
"""
from __future__ import annotations

import json
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

MODEL_DIR = Path(__file__).parent / "saved"
MODEL_PATH = MODEL_DIR / "deterioration_model.joblib"
METRICS_PATH = MODEL_DIR / "deterioration_model.metrics.json"
N_INSTALMENT_ROWS = 3_000_000
N_CARD_ROWS = 1_500_000
HORIZON = 180

FEATURES = [
    "late_ratio_3m", "late_ratio_prev3m", "late_ratio_change",
    "max_dpd_3m", "max_dpd_prev3m", "current_dpd",
    "shortfall_3m", "shortfall_prev3m", "n_instalments_6m",
    "card_util", "card_util_change", "instalment_to_income",
]
FAMILY = {
    "late_ratio_3m": "repayment", "late_ratio_prev3m": "repayment", "late_ratio_change": "repayment",
    "max_dpd_3m": "repayment", "max_dpd_prev3m": "repayment", "current_dpd": "repayment",
    "shortfall_3m": "repayment", "shortfall_prev3m": "repayment", "n_instalments_6m": "repayment",
    "card_util": "exposure", "card_util_change": "exposure", "instalment_to_income": "affordability",
}
LABELS = {
    "late_ratio_3m": "late share (last 3 months)", "late_ratio_prev3m": "late share (3-6 months ago)",
    "late_ratio_change": "change in late share", "max_dpd_3m": "worst DPD (last 3 months)",
    "max_dpd_prev3m": "worst DPD (3-6 months ago)", "current_dpd": "current days past due",
    "shortfall_3m": "unpaid share (last 3 months)", "shortfall_prev3m": "unpaid share (3-6 months ago)",
    "n_instalments_6m": "instalments due (6 months)", "card_util": "card utilisation",
    "card_util_change": "change in card utilisation", "instalment_to_income": "instalment to income",
}


def snapshot_features(inst: pd.DataFrame, cards: pd.DataFrame, income: pd.Series, t: pd.Series) -> pd.DataFrame:
    """inst: customer_id, due, paid (days; NaN = unpaid), amount_due, amount_paid.
    cards: customer_id, day, balance, limit. income: monthly income by customer.
    t: cutoff day per customer. Same function for Home Credit and the platform."""
    out = pd.DataFrame(index=t.index)
    if not inst.empty:
        df = inst.join(t.rename("T"), on="customer_id")
        df = df[df["due"] <= df["T"]]
        paid_by_t = df["paid"].notna() & (df["paid"] <= df["T"])
        df["dpd"] = np.where(paid_by_t, (df["paid"] - df["due"]).clip(lower=0), df["T"] - df["due"])
        df["paid_amt"] = np.where(paid_by_t, df["amount_paid"], 0.0)
        age = df["T"] - df["due"]
        w3, w6 = df[age < 90], df[(age >= 90) & (age < 180)]

        def agg(w, sfx):
            g = w.groupby("customer_id")
            r = pd.DataFrame({f"late_ratio_{sfx}": g["dpd"].apply(lambda s: (s > 0).mean()),
                              f"max_dpd_{sfx}": g["dpd"].max(),
                              f"shortfall_{sfx}": (1 - g["paid_amt"].sum() / g["amount_due"].sum().replace(0, np.nan)).clip(0, 1)})
            return r
        out = out.join(agg(w3, "3m")).join(agg(w6, "prev3m"))
        unpaid = df[~paid_by_t]
        out["current_dpd"] = unpaid.groupby("customer_id")["dpd"].max()
        out["current_dpd"] = out["current_dpd"].fillna(0)
        out["n_instalments_6m"] = df[age < 180].groupby("customer_id").size()
        out["late_ratio_change"] = out["late_ratio_3m"] - out["late_ratio_prev3m"]
        last_due = df[age < 90].groupby("customer_id")["amount_due"].mean()
        out["instalment_to_income"] = (last_due / income.reindex(out.index)).clip(upper=10)
    if not cards.empty:
        c = cards.join(t.rename("T"), on="customer_id")
        c = c[c["day"] <= c["T"]]
        c["util"] = (c["balance"] / c["limit"].where(c["limit"] > 0)).clip(0, 2)
        now = c[c["day"] > c["T"] - 30].groupby("customer_id")["util"].mean()
        ago = c[(c["day"] <= c["T"] - 60) & (c["day"] > c["T"] - 120)].groupby("customer_id")["util"].mean()
        out["card_util"] = now
        out["card_util_change"] = now - ago
    for f in FEATURES:
        if f not in out:
            out[f] = np.nan
    return out[FEATURES].astype(float)


def _load(data_dir: Path):
    inst = pd.read_csv(data_dir / "installments_payments.csv", nrows=N_INSTALMENT_ROWS, usecols=[
        "SK_ID_PREV", "SK_ID_CURR", "NUM_INSTALMENT_NUMBER", "DAYS_INSTALMENT", "DAYS_ENTRY_PAYMENT",
        "AMT_INSTALMENT", "AMT_PAYMENT"])
    inst = inst.groupby(["SK_ID_CURR", "SK_ID_PREV", "NUM_INSTALMENT_NUMBER"], sort=False).agg(
        due=("DAYS_INSTALMENT", "min"), paid=("DAYS_ENTRY_PAYMENT", "max"),
        amount_due=("AMT_INSTALMENT", "max"), amount_paid=("AMT_PAYMENT", "sum")).reset_index()
    inst = inst.rename(columns={"SK_ID_CURR": "customer_id"})[["customer_id", "due", "paid", "amount_due", "amount_paid"]]
    cards = pd.read_csv(data_dir / "credit_card_balance.csv", nrows=N_CARD_ROWS, usecols=[
        "SK_ID_CURR", "MONTHS_BALANCE", "AMT_BALANCE", "AMT_CREDIT_LIMIT_ACTUAL", "SK_DPD"])
    cards = pd.DataFrame({"customer_id": cards["SK_ID_CURR"], "day": cards["MONTHS_BALANCE"] * 30.0,
                          "balance": cards["AMT_BALANCE"], "limit": cards["AMT_CREDIT_LIMIT_ACTUAL"],
                          "card_dpd": cards["SK_DPD"]})
    app = pd.read_csv(data_dir / "application_train.csv", usecols=["SK_ID_CURR", "AMT_INCOME_TOTAL"])
    income = (app.set_index("SK_ID_CURR")["AMT_INCOME_TOTAL"] / 12)
    return inst, cards, income


def _ccf(cards: pd.DataFrame) -> float:
    """Share of the undrawn limit drawn in the 6 months before a card goes 30+ DPD."""
    c = cards.sort_values("day")
    rows = []
    for _, g in c.groupby("customer_id"):
        hit = g[g["card_dpd"] > 30]
        if hit.empty:
            continue
        d = hit["day"].iloc[0]
        before = g[(g["day"] <= d - 180)]
        if before.empty:
            continue
        b0, lim = before["balance"].iloc[-1], before["limit"].iloc[-1]
        if lim > b0 > -1:
            rows.append(np.clip((hit["balance"].iloc[0] - b0) / (lim - b0), 0, 1))
    return float(np.mean(rows)) if rows else float("nan"), len(rows)


def train(data_dir: str) -> dict:
    import xgboost as xgb
    from sklearn.metrics import roc_auc_score
    from sklearn.model_selection import train_test_split
    from sklearn.tree import DecisionTreeClassifier

    d = Path(data_dir)
    inst, cards, income = _load(d)
    span = inst.groupby("customer_id")["due"].agg(["min", "max"])
    span = span[(span["max"] - span["min"]) >= 360 + HORIZON]
    t = (span["max"] - HORIZON).rename("T")
    X = snapshot_features(inst, cards, income, t)
    fut = inst.join(t, on="customer_id", how="inner")
    fut = fut[(fut["due"] > fut["T"]) & (fut["due"] <= fut["T"] + HORIZON)]
    late = (fut["paid"].isna() & (fut["T"] + HORIZON - fut["due"] > 30)) | ((fut["paid"] - fut["due"]) > 30)
    y = late.groupby(fut["customer_id"]).any().reindex(X.index).fillna(False).astype(int)

    Xtr, Xte, ytr, yte = train_test_split(X, y, test_size=0.25, random_state=42, stratify=y)
    model = xgb.XGBClassifier(n_estimators=300, max_depth=4, learning_rate=0.05, subsample=0.8,
                              colsample_bytree=0.8, eval_metric="logloss", n_jobs=-1)
    model.fit(Xtr, ytr)
    p = model.predict_proba(Xte)[:, 1]
    auc = roc_auc_score(yte, p)
    base = float(y.mean())

    # Trend cut points learned from data: change in log-odds over 3 months vs outcome
    X90 = snapshot_features(inst, cards, income, t - 90)
    lo = lambda q: np.log(np.clip(q, 1e-4, 1 - 1e-4) / (1 - np.clip(q, 1e-4, 1 - 1e-4)))
    delta = lo(model.predict_proba(X)[:, 1]) - lo(model.predict_proba(X90.reindex(X.index))[:, 1])
    tree = DecisionTreeClassifier(max_leaf_nodes=3, min_samples_leaf=max(200, len(X) // 20)).fit(delta.reshape(-1, 1), y)
    cuts = sorted(float(v) for v in tree.tree_.threshold if v != -2)
    ccf, ccf_n = _ccf(cards)

    metrics = {
        "dataset": f"Home Credit, first {N_INSTALMENT_ROWS:,} instalment rows + {N_CARD_ROWS:,} card rows (sampled for speed)",
        "customers": int(len(X)), "target": "instalment > 30 days past due within 180 days (RBI SMA-1)",
        "base_rate": round(base, 4), "test_auc": round(float(auc), 4),
        "trend_cuts_logodds": [round(c, 4) for c in cuts],
        "ccf_undrawn": round(ccf, 4), "ccf_cards": ccf_n,
    }
    MODEL_DIR.mkdir(exist_ok=True)
    joblib.dump({"model": model, "features": FEATURES, "base_rate": base, "trend_cuts": cuts, "ccf": ccf}, MODEL_PATH)
    METRICS_PATH.write_text(json.dumps(metrics, indent=2))
    print(json.dumps(metrics, indent=2))
    return metrics


_CACHE: dict = {}


def load():
    if "m" not in _CACHE:
        _CACHE["m"] = joblib.load(MODEL_PATH)
    return _CACHE["m"]


def explain(X: pd.DataFrame) -> list[dict]:
    """Probability + per-family SHAP contributions + cross-family interactions (log-odds)."""
    import xgboost as xgb
    m = load()
    booster = m["model"].get_booster()
    dm = xgb.DMatrix(X[FEATURES].astype(float), feature_names=FEATURES)
    probs = m["model"].predict_proba(X[FEATURES].astype(float))[:, 1]
    inter = booster.predict(dm, pred_interactions=True)
    fams = sorted(set(FAMILY.values()))
    out = []
    for i in range(len(X)):
        M = inter[i]
        fam_main = {f: 0.0 for f in fams}
        cross = {}
        feat = {}
        for a, fa in enumerate(FEATURES):
            feat[fa] = float(M[a, :-1].sum())
            for b, fb in enumerate(FEATURES):
                if FAMILY[fa] == FAMILY[fb]:
                    fam_main[FAMILY[fa]] += float(M[a, b])
                else:
                    key = "+".join(sorted((FAMILY[fa], FAMILY[fb])))
                    cross[key] = cross.get(key, 0.0) + float(M[a, b])
        out.append({"p": float(probs[i]), "base_rate": m["base_rate"], "family": fam_main,
                    "interactions": cross, "features": feat, "bias": float(M[-1, -1])})
    return out


if __name__ == "__main__":
    train(str(Path(__file__).resolve().parents[1] / "data" / "raw"))
