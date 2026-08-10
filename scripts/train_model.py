"""Entrena el modelo de riesgo y lo exporta como JSON de coeficientes.

La configuración que se usa aquí quedó estipulada en `notebooks/02_modelado.ipynb`. El archivo
resultante, `artifacts/model.json`, contiene todo lo que el servidor MCP necesita para puntuar una
transacción sin instalar scikit-learn.

Antes de terminar, el script **verifica la paridad de inferencia**, reimplementa la evaluación del
modelo en Python puro leyendo el JSON exportado y comprueba que produzca el mismo puntaje que
scikit-learn. Es la garantía de que la versión del servidor dará resultados idénticos.

Uso:
    uv run python scripts/train_model.py
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    average_precision_score,
    f1_score,
    precision_score,
    recall_score,
    roc_auc_score,
)
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

# Ambos scripts viven en la misma carpeta, así que el contrato de variables se importa en
# lugar de duplicarse.
from prepare_dataset import (
    CATEGORIES,
    FEATURES,
    NEUTRAL_AMT_RATIO,
    NEUTRAL_MINUTES,
    NIGHT_HOURS,
)

ROOT = Path(__file__).resolve().parents[1]
PROCESSED = ROOT / "data" / "processed"
ARTIFACTS = ROOT / "artifacts"

# Configuración estipulada en el notebook de modelado.
THRESHOLD = 0.90
RANDOM_STATE = 42
MAX_ITER = 1000

# Filas que se usan para comprobar la paridad entre scikit-learn y el cálculo manual.
PARITY_SAMPLE = 5_000
PARITY_TOLERANCE = 1e-9


def build_model() -> Pipeline:
    """Devuelve el modelo: escalado de variables y regresión logística compensada."""
    return Pipeline([
        ("scaler", StandardScaler()),
        ("logistic", LogisticRegression(
            class_weight="balanced",
            max_iter=MAX_ITER,
            random_state=RANDOM_STATE,
        )),
    ])


def evaluate(model: Pipeline, X: pd.DataFrame, y: pd.Series, days: int) -> dict:
    """Calcula las métricas de la partición de prueba con el umbral elegido."""
    probability = model.predict_proba(X)[:, 1]
    prediction = (probability >= THRESHOLD).astype(int)
    alerts = int(prediction.sum())

    return {
        "threshold": THRESHOLD,
        "roc_auc": float(roc_auc_score(y, probability)),
        "pr_auc": float(average_precision_score(y, probability)),
        "precision": float(precision_score(y, prediction, zero_division=0)),
        "recall": float(recall_score(y, prediction)),
        "f1": float(f1_score(y, prediction)),
        "alerts": alerts,
        "alerts_per_day": alerts / days,
        "frauds_detected": int(((prediction == 1) & (y == 1)).sum()),
        "frauds_missed": int(((prediction == 0) & (y == 1)).sum()),
        "false_alarms": int(((prediction == 1) & (y == 0)).sum()),
    }


def export(model: Pipeline, metrics: dict, rows: dict) -> dict:
    """Arma el diccionario que se guarda en artifacts/model.json."""
    scaler: StandardScaler = model.named_steps["scaler"]
    logistic: LogisticRegression = model.named_steps["logistic"]

    return {
        "version": 1,
        "trained_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model": "logistic_regression",
        "description": (
            "Puntaje de riesgo de fraude. El servidor estandariza cada variable con "
            "(valor - mean) / scale, suma los productos con coefficients, agrega intercept "
            "y aplica la sigmoide 1 / (1 + exp(-z))."
        ),
        # Contrato de variables: nombres en el orden exacto en que deben construirse.
        "features": FEATURES,
        "categories": CATEGORIES,
        "night_hours": sorted(NIGHT_HOURS),
        "neutral_values": {
            "amt_ratio_prev_mean": NEUTRAL_AMT_RATIO,
            "minutes_since_prev": NEUTRAL_MINUTES,
        },
        "scaler": {
            "mean": [float(v) for v in scaler.mean_],
            "scale": [float(v) for v in scaler.scale_],
        },
        "coefficients": [float(v) for v in logistic.coef_[0]],
        "intercept": float(logistic.intercept_[0]),
        "threshold": THRESHOLD,
        "training": {
            "validation": "temporal split: train 2019-01→2020-06, test 2020-06→2020-12",
            "class_weight": "balanced",
            "random_state": RANDOM_STATE,
            **rows,
        },
        "metrics": metrics,
    }


def score_with_pure_python(artifact: dict, row: dict) -> float:
    """Evalúa el modelo leyendo solo el JSON exportado, tal como lo hará el servidor."""
    z = artifact["intercept"]
    for name, mean, scale, coefficient in zip(
        artifact["features"],
        artifact["scaler"]["mean"],
        artifact["scaler"]["scale"],
        artifact["coefficients"],
    ):
        z += ((row[name] - mean) / scale) * coefficient
    return 1.0 / (1.0 + math.exp(-z))


def check_parity(model: Pipeline, artifact: dict, X: pd.DataFrame) -> float:
    """Compara el puntaje de scikit-learn con el cálculo manual y devuelve la diferencia máxima."""
    sample = X.head(PARITY_SAMPLE)
    expected = model.predict_proba(sample)[:, 1]
    obtained = [score_with_pure_python(artifact, row) for row in sample.to_dict("records")]
    return max(abs(a - b) for a, b in zip(expected, obtained))


def main() -> None:
    train = pd.read_csv(PROCESSED / "train.csv")
    test = pd.read_csv(PROCESSED / "test.csv")

    if list(train.columns) != [*FEATURES, "is_fraud"]:
        raise ValueError(
            "Las columnas del dataset no coinciden con el contrato de variables. "
            "Volver a ejecutar scripts/prepare_dataset.py"
        )

    X_train, y_train = train[FEATURES], train["is_fraud"]
    X_test, y_test = test[FEATURES], test["is_fraud"]

    print(f"entrenando con {len(train):,} filas y {len(FEATURES)} variables")
    model = build_model()
    model.fit(X_train, y_train)

    # Los 193 días del período de prueba se toman del archivo crudo, para expresar el
    # volumen de alertas en términos diarios.
    dates = pd.read_csv(
        ROOT / "data" / "raw" / "fraudTest.csv",
        usecols=["trans_date_trans_time"],
        parse_dates=["trans_date_trans_time"],
    )["trans_date_trans_time"]
    days = (dates.max() - dates.min()).days

    metrics = evaluate(model, X_test, y_test, days)
    artifact = export(model, metrics, {
        "train_rows": len(train),
        "train_fraud_rate": float(y_train.mean()),
        "test_rows": len(test),
        "test_fraud_rate": float(y_test.mean()),
        "test_days": days,
    })

    difference = check_parity(model, artifact, X_test)
    if difference > PARITY_TOLERANCE:
        raise ValueError(
            f"El cálculo en Python puro difiere de scikit-learn en {difference:.3e}, "
            f"por encima de la tolerancia de {PARITY_TOLERANCE:.0e}"
        )

    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    destination = ARTIFACTS / "model.json"
    destination.write_text(json.dumps(artifact, indent=2) + "\n", encoding="utf-8")

    print(f"\numbral {THRESHOLD}")
    print(f"  ROC-AUC   {metrics['roc_auc']:.4f}")
    print(f"  PR-AUC    {metrics['pr_auc']:.4f}")
    print(f"  precisión {metrics['precision']:.4f}")
    print(f"  recall    {metrics['recall']:.4f}")
    print(f"  F1        {metrics['f1']:.4f}")
    print(
        f"  {metrics['frauds_detected']:,} fraudes detectados, "
        f"{metrics['frauds_missed']:,} sin detectar, "
        f"{metrics['alerts_per_day']:.0f} alertas por día"
    )
    print(f"\nparidad con el cálculo en Python puro: diferencia máxima {difference:.2e}")
    print(f"escrito {destination.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
