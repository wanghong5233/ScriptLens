from __future__ import annotations

import hashlib
from collections import Counter
from functools import lru_cache
from typing import Iterable

import numpy as np


@lru_cache(maxsize=1)
def _load_sklearn_metrics():
    try:
        from sklearn.metrics import cohen_kappa_score, confusion_matrix, f1_score

        return cohen_kappa_score, confusion_matrix, f1_score
    except Exception:
        return None


@lru_cache(maxsize=1)
def _load_embedder():
    try:
        from sentence_transformers import SentenceTransformer

        return SentenceTransformer("BAAI/bge-small-zh-v1.5")
    except Exception:
        return None


def krippendorff_alpha(matrix: list[list[str]]) -> float:
    if not matrix:
        return 0.0
    try:
        import krippendorff

        arr = np.array(matrix, dtype=object)
        # reliability_data accepts [raters, items]
        alpha = krippendorff.alpha(reliability_data=arr, level_of_measurement="nominal")
        return float(alpha if alpha is not None else 0.0)
    except Exception:
        return 0.0


def cohen_kappa_mean(matrix: list[list[str]]) -> float:
    if len(matrix) < 2:
        return 0.0
    sklearn_metrics = _load_sklearn_metrics()
    if sklearn_metrics is None:
        return pairwise_agreement_rate(matrix)
    cohen_kappa_score = sklearn_metrics[0]
    kappas: list[float] = []
    for i in range(len(matrix)):
        for j in range(i + 1, len(matrix)):
            try:
                kappas.append(float(cohen_kappa_score(matrix[i], matrix[j])))
            except Exception:
                kappas.append(0.0)
    return float(np.mean(kappas)) if kappas else 0.0


def pairwise_agreement_rate(matrix: list[list[str]]) -> float:
    if len(matrix) < 2:
        return 0.0
    rates: list[float] = []
    for i in range(len(matrix)):
        for j in range(i + 1, len(matrix)):
            if not matrix[i]:
                rates.append(0.0)
                continue
            same = sum(1 for a, b in zip(matrix[i], matrix[j]) if a == b)
            rates.append(same / len(matrix[i]))
    return float(np.mean(rates)) if rates else 0.0


def _hash_embed(text: str, dim: int = 32) -> np.ndarray:
    h = hashlib.sha256(text.encode("utf-8")).digest()
    arr = np.frombuffer(h, dtype=np.uint8).astype(np.float32)
    arr = arr[:dim]
    norm = np.linalg.norm(arr)
    if norm == 0:
        return arr
    return arr / norm


def _embed_values(values: list[str]) -> np.ndarray:
    model = _load_embedder()
    if model is None:
        return np.stack([_hash_embed(v) for v in values], axis=0)
    embs = model.encode(values, normalize_embeddings=True)
    return np.array(embs, dtype=np.float32)


def cosine_similarity_enum(matrix: list[list[str]]) -> float:
    if len(matrix) < 2:
        return 0.0
    # Flatten pairwise same-position labels into text pairs.
    sims: list[float] = []
    for i in range(len(matrix)):
        for j in range(i + 1, len(matrix)):
            row_i, row_j = matrix[i], matrix[j]
            if not row_i:
                continue
            emb_i = _embed_values(row_i)
            emb_j = _embed_values(row_j)
            row_sims = (emb_i * emb_j).sum(axis=1)
            sims.append(float(np.mean(row_sims)))
    return float(np.mean(sims)) if sims else 0.0


def macro_f1_against_gold(pred: Iterable[str], gold: Iterable[str]) -> float:
    pred_list = list(pred)
    gold_list = list(gold)
    if not pred_list or not gold_list or len(pred_list) != len(gold_list):
        return 0.0
    labels = sorted(set(pred_list) | set(gold_list))
    sklearn_metrics = _load_sklearn_metrics()
    if sklearn_metrics is None:
        return 0.0
    f1_score = sklearn_metrics[2]
    try:
        return float(f1_score(gold_list, pred_list, labels=labels, average="macro"))
    except Exception:
        return 0.0


def confusion_matrix_dict(pred: Iterable[str], gold: Iterable[str]) -> dict:
    pred_list = list(pred)
    gold_list = list(gold)
    if not pred_list or not gold_list or len(pred_list) != len(gold_list):
        return {"labels": [], "matrix": []}
    labels = sorted(set(pred_list) | set(gold_list))
    sklearn_metrics = _load_sklearn_metrics()
    if sklearn_metrics is None:
        return {"labels": labels, "matrix": []}
    confusion_matrix = sklearn_metrics[1]
    mat = confusion_matrix(gold_list, pred_list, labels=labels)
    return {"labels": labels, "matrix": mat.tolist()}


def majority_vote(values_by_run: list[list[str]]) -> list[str]:
    if not values_by_run:
        return []
    n_items = len(values_by_run[0])
    out: list[str] = []
    for col in range(n_items):
        candidates = [row[col] for row in values_by_run if len(row) > col and row[col] != ""]
        if not candidates:
            out.append("")
            continue
        out.append(Counter(candidates).most_common(1)[0][0])
    return out
