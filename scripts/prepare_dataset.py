"""Construye el dataset que alimenta el entrenamiento del modelo de riesgo.

Lee los dos archivos crudos de `data/raw/` y escribe `data/processed/train.csv` y
`data/processed/test.csv` con las variables ya construidas más la etiqueta `is_fraud`.

Cada transformación implementa una decisión documentada en `notebooks/01_eda.ipynb`;
la sección correspondiente se cita en el comentario de cada bloque.

Uso:
    uv run python scripts/prepare_dataset.py
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
PROCESSED = ROOT / "data" / "processed"

# Solo estas columnas del archivo crudo se usan. Se descartan las que identifican al
# titular (first, last, street, city, zip, job), el identificador de la transacción,
# el comercio individual (693 niveles), las coordenadas (la distancia cliente-comercio
# no discrimina) y unix_time, que no concuerda con la fecha (EDA #3, #7, #8, #10).
RAW_COLUMNS = [
    "trans_date_trans_time",
    "cc_num",
    "category",
    "amt",
    "gender",
    "city_pop",
    "dob",
    "is_fraud",
]

# Vocabulario fijo de categorías, en orden alfabético. Queda congelado aquí para que el
# servidor construya siempre las mismas columnas one-hot en el mismo orden (EDA #7).
CATEGORIES = [
    "entertainment",
    "food_dining",
    "gas_transport",
    "grocery_net",
    "grocery_pos",
    "health_fitness",
    "home",
    "kids_pets",
    "misc_net",
    "misc_pos",
    "personal_care",
    "shopping_net",
    "shopping_pos",
    "travel",
]

# La franja nocturna va de las 22:00 a las 03:59, que es donde el EDA encontró la
# concentración de fraude (EDA #6).
NIGHT_HOURS = {22, 23, 0, 1, 2, 3}

# Valores neutros para la primera transacción de cada tarjeta, que no tiene historial:
# se declara que gastó lo habitual y que pasó mucho tiempo desde el movimiento anterior.
# El servidor aplicará exactamente los mismos valores al puntuar una tarjeta sin historia.
NEUTRAL_AMT_RATIO = 1.0
NEUTRAL_MINUTES = 43_200.0  # 30 días

# Orden canónico de las variables. Es el contrato que el modelo exporta y que el
# servidor debe reproducir al construir el vector de entrada.
FEATURES = [
    "log_amt",
    "hour",
    "is_night",
    "day_of_week",
    "is_weekend",
    "age",
    "log_city_pop",
    "gender_m",
    "amt_ratio_prev_mean",
    "minutes_since_prev",
    *[f"cat_{name}" for name in CATEGORIES],
]


def load_raw(filename: str, split: str) -> pd.DataFrame:
    """Carga un archivo crudo con tipos explícitos y le marca la partición a la que pertenece."""
    df = pd.read_csv(
        RAW / filename,
        # `usecols` deja fuera todo lo que no se necesita, incluida la primera columna del
        # CSV, que es un índice sin nombre. El índice resultante es el número de fila del
        # archivo, y sirve para devolver las filas a su orden original al final.
        usecols=lambda name: name in RAW_COLUMNS,
        dtype={
            "cc_num": "int64",
            "category": "category",
            "amt": "float64",
            "gender": "category",
            "city_pop": "int32",
            "is_fraud": "int8",
        },
        parse_dates=["trans_date_trans_time", "dob"],
    )
    df["split"] = split
    print(f"  {filename}: {len(df):,} filas")
    return df


def add_history_features(df: pd.DataFrame) -> pd.DataFrame:
    """Agrega las variables que comparan cada transacción con el pasado de su tarjeta.

    Ambas se calculan usando únicamente transacciones anteriores de la misma tarjeta, de
    modo que ninguna fila se mire a sí misma ni al futuro (EDA #9). Solo intervienen montos
    y marcas de tiempo, la etiqueta `is_fraud` nunca participa, así que calcularlas sobre la
    unión de las dos particiones no filtra información de la partición de prueba.
    """
    df = df.sort_values(["cc_num", "trans_date_trans_time"])
    by_card = df.groupby("cc_num", observed=True, sort=False)

    # Media de los montos ANTERIORES: la suma acumulada menos el monto actual, dividida
    # entre la cantidad de transacciones previas. Equivale a una media expansiva desplazada
    # una posición, pero sin recorrer grupo por grupo.
    previous_sum = by_card["amt"].cumsum() - df["amt"]
    previous_count = by_card.cumcount()
    previous_mean = previous_sum.where(previous_count > 0) / previous_count.replace(0, np.nan)
    minutes = by_card["trans_date_trans_time"].diff().dt.total_seconds().div(60)

    # Una media previa de cero produciría infinito; ocurre solo si todas las transacciones
    # anteriores fueron de monto cero, y se trata como si no hubiera historial.
    ratio = (df["amt"] / previous_mean).replace([np.inf, -np.inf], np.nan)

    df["amt_ratio_prev_mean"] = ratio.fillna(NEUTRAL_AMT_RATIO)
    df["minutes_since_prev"] = minutes.fillna(NEUTRAL_MINUTES)
    return df


def add_transaction_features(df: pd.DataFrame) -> pd.DataFrame:
    """Agrega las variables que se derivan de la transacción misma."""
    timestamp = df["trans_date_trans_time"]

    df["log_amt"] = np.log1p(df["amt"])  # el monto tiene cola larga (EDA #5)
    df["hour"] = timestamp.dt.hour
    df["is_night"] = timestamp.dt.hour.isin(NIGHT_HOURS).astype("int8")
    df["day_of_week"] = timestamp.dt.dayofweek
    df["is_weekend"] = (timestamp.dt.dayofweek >= 5).astype("int8")
    df["age"] = (timestamp - df["dob"]).dt.days / 365.25
    df["log_city_pop"] = np.log1p(df["city_pop"])
    df["gender_m"] = (df["gender"] == "M").astype("int8")

    # One-hot de la categoría con el vocabulario congelado, no con lo que traiga el archivo.
    for name in CATEGORIES:
        df[f"cat_{name}"] = (df["category"] == name).astype("int8")

    return df


def main() -> None:
    print("Cargando archivos crudos")
    train = load_raw("fraudTrain.csv", "train")
    test = load_raw("fraudTest.csv", "test")

    observed = set(train["category"].cat.categories) | set(test["category"].cat.categories)
    if observed != set(CATEGORIES):
        raise ValueError(
            "El vocabulario de categorías cambió respecto al congelado en el script: "
            f"{sorted(observed ^ set(CATEGORIES))}"
        )

    print("Construyendo variables de historial sobre la unión de ambas particiones")
    combined = add_history_features(pd.concat([train, test], ignore_index=False))

    print("Construyendo variables de la transacción")
    combined = add_transaction_features(combined)

    PROCESSED.mkdir(parents=True, exist_ok=True)
    for split in ("train", "test"):
        part = combined.loc[combined["split"] == split, [*FEATURES, "is_fraud"]]
        part = part.sort_index()

        if part.isna().any().any():
            raise ValueError(f"La partición {split} contiene valores faltantes")

        destination = PROCESSED / f"{split}.csv"
        part.to_csv(destination, index=False, float_format="%.6g")
        rate = part["is_fraud"].mean()
        print(
            f"  {destination.relative_to(ROOT)}: {len(part):,} filas × {part.shape[1]} columnas, "
            f"fraude {rate:.4%}"
        )

    print(f"\n{len(FEATURES)} variables: {', '.join(FEATURES)}")


if __name__ == "__main__":
    main()
