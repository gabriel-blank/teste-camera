# classifier_model_training.py
# Requisitos:
# pip install torch torchvision pillow opencv-python requests numpy scikit-learn joblib

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from joblib import dump
from sklearn.decomposition import PCA
from sklearn.metrics import accuracy_score, classification_report, f1_score
from sklearn.model_selection import GridSearchCV, StratifiedKFold, train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.svm import SVC

from utils.api_controller import ApiController
from utils.logger import logger

# >>> módulo compartilhado de features (novo)
from utils.feature_extractor import (
    ResNetFeature,
    normalize_polygon,
    resolve_image_url,
    load_image,
)


# ---------------------------------------------------------------------
# Treino end-to-end do classificador (SVM) a partir das anotações da API
# ---------------------------------------------------------------------
def train_svm_end_to_end(
    api: ApiController,
    po_list: List[int],
    base_url_prefix: str = "",
    folder: str = "anomalias",
    include_background: bool = True,
    normal_id: int = 0,
    normal_name: str = "normal",
    margin_cells: int = 1,
    use_pca: bool = True,
    random_state: int = 42,
    save_model_to: str | None = "model/svm/model.joblib",
    save_artifacts: bool = False,
    artifacts_dir: str = "dataset",
):
    """
    Faz TUDO em uma chamada:
      1) lista anotações dos POs (folder='anomalias')
      2) constrói X,y em memória (com/sem fundo)
      3) treina SVM com GridSearchCV
      4) salva modelo e (opcionalmente) manifest/class_map/metrics

    Retorna: (modelo_pipeline, meta_dict)
    """

    # =========================
    # 1) Coleta (manifest in-mem)
    # =========================
    rows = _collect_manifest_in_memory(api, po_list=po_list, folder=folder)
    if not rows:
        raise RuntimeError("Nenhum item classificado encontrado para treino.")

    # ==========================
    # 2) X,y (+ class_map) in-mem
    # ==========================
    X, y, class_map = _build_xy(
        rows,
        base_url_prefix=base_url_prefix,
        include_background=include_background,
        normal_id=normal_id,
        normal_name=normal_name,
        margin_cells=margin_cells,
        device=None,
    )

    # ============
    # 3) Treino
    # ============
    model, meta = _train_svm(X, y, use_pca=use_pca, random_state=random_state)
    meta["class_map"] = class_map

    # ===========================
    # 4) Salvar artefatos (opt.)
    # ===========================
    if save_artifacts:
        os.makedirs(artifacts_dir, exist_ok=True)

        # manifest.jsonl
        manifest_path = os.path.join(artifacts_dir, "manifest.jsonl")
        with open(manifest_path, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

        # class_map.json
        cm_path = os.path.join(artifacts_dir, "class_map.json")
        with open(cm_path, "w", encoding="utf-8") as f:
            json.dump(class_map, f, ensure_ascii=False, indent=2)

        # meta.json (na mesma pasta do modelo, se houver)
        meta_dir = os.path.dirname(save_model_to or artifacts_dir) or "."
        os.makedirs(meta_dir, exist_ok=True)
        meta_path = os.path.join(meta_dir, "model_meta.json")
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

    # ========================
    # 5) Salvar modelo (opt.)
    # ========================
    if save_model_to:
        model_dir = os.path.dirname(save_model_to) or "."
        os.makedirs(model_dir, exist_ok=True)
        dump(model, save_model_to)
        logger.info(f"Modelo salvo em: {save_model_to}")

    return model, meta


# ---------------------------------------------------------------------
# Helpers internos (manifest, construção de X/y e treino SVM)
# ---------------------------------------------------------------------
def _collect_manifest_in_memory(
    api: ApiController,
    po_list: List[int],
    folder: str = "anomalias",
) -> List[Dict[str, Any]]:
    """
    Retorna uma lista de entradas normalizadas:
      { id, po, image_url, polygon_norm, classId, className, timestamp, ... }
    """
    rows: List[Dict[str, Any]] = []
    seen: set[Tuple[str | None, str | None]] = set()

    def _normalize(raw: Dict[str, Any], po: int) -> Dict[str, Any] | None:
        poly = raw.get("polygon_norm") or raw.get("polygon")
        cid = raw.get("classId")
        url = raw.get("url") or raw.get("image_url")
        if poly is None or cid is None or not url:
            return None
        return {
            "id": raw.get("id") or raw.get("name"),
            "po": raw.get("po", po),
            "image_url": url,
            "polygon_norm": poly,
            "classId": int(cid),
            "className": raw.get("className"),
            "timestamp": raw.get("timestamp"),
            "folder": raw.get("folder"),
            "name": raw.get("name"),
            "isClassified": raw.get("isClassified", True),
        }

    for po in po_list:
        data = api.list_images(po=po, folder=folder)
        for r in data:
            if r.get("isClassified") is False:
                continue
            item = _normalize(r, po)
            if not item:
                continue
            key = (item["id"], item["image_url"])
            if key in seen:
                continue
            seen.add(key)
            rows.append(item)

    logger.info(f"Coletados {len(rows)} itens classificados de {len(po_list)} POs.")
    return rows


def _build_xy(
    rows: List[Dict[str, Any]],
    base_url_prefix: str = "",
    include_background: bool = True,
    normal_id: int = 0,
    normal_name: str = "normal",
    margin_cells: int = 1,
    device: Optional[str] = None,
) -> Tuple[np.ndarray, np.ndarray, Dict[str, str]]:
    """
    Constrói X,y a partir das linhas do manifest (em memória).
    Se include_background=True, adiciona amostras de fundo como 'normal_id'.
    Retorna: X [N, 2048], y [N], class_map {id->name}
    """
    extractor = ResNetFeature(device=device)
    X_list: List[np.ndarray] = []
    y_list: List[int] = []
    class_map: Dict[str, str] = {}

    dropped = 0
    for r in rows:
        url = r.get("image_url")
        raw_poly = r.get("polygon_norm") or r.get("polygon")
        cid = r.get("classId")
        cname = r.get("className") or f"class_{cid}"
        poly = normalize_polygon(raw_poly)
        if not url or cid is None or poly is None:
            dropped += 1
            continue

        full = resolve_image_url(url, base_url_prefix=base_url_prefix)
        try:
            img = load_image(full)

            if include_background:
                emb_poly, emb_bg = extractor.region_and_background_embeddings(
                    img, poly, margin_cells=margin_cells
                )
                # poly
                X_list.append(emb_poly.astype(np.float32))
                y_list.append(int(cid))
                # background
                X_list.append(emb_bg.astype(np.float32))
                y_list.append(int(normal_id))
            else:
                emb = extractor.region_embedding(img, poly)
                X_list.append(emb.astype(np.float32))
                y_list.append(int(cid))

            class_map[str(int(cid))] = cname

        except Exception as e:
            logger.warning(f"Pulando {full}: {e}")
            dropped += 1

    if include_background:
        class_map[str(normal_id)] = normal_name

    if not X_list:
        raise RuntimeError("Nenhuma amostra válida encontrada.")

    X = np.stack(X_list, axis=0).astype(np.float32)
    y = np.array(y_list, dtype=np.int64)

    logger.info(
        f"X={X.shape}{' (inclui fundo)' if include_background else ''} | y={y.shape} | descartadas={dropped}"
    )

    return X, y, class_map


def _train_svm(
    X: np.ndarray,
    y: np.ndarray,
    use_pca: bool = True,
    random_state: int = 42,
) -> Tuple[Pipeline, Dict[str, Any]]:
    """
    Treina um Pipeline [Scaler -> (PCA?) -> SVC] com GridSearchCV
    e validação estratificada. Retorna (best_estimator, meta).
    """
    # split estratificado (um pouco mais robusto para bases pequenas)
    test_size = 0.2 if len(y) >= 20 else 0.25
    Xtr, Xva, ytr, yva = train_test_split(
        X, y, test_size=test_size, stratify=y, random_state=random_state
    )

    steps = [("scaler", StandardScaler())]
    if use_pca:
        steps.append(
            (
                "pca",
                PCA(
                    n_components=0.95,
                    svd_solver="full",
                    random_state=random_state,
                ),
            )
        )
    steps.append(("svm", SVC(kernel="rbf", probability=True, class_weight="balanced")))
    pipe = Pipeline(steps)

    # CV segura para base menor
    _, counts = np.unique(ytr, return_counts=True)
    min_per_class = counts.min()
    n_splits = max(2, min(5, int(min_per_class)))

    cv = StratifiedKFold(n_splits=n_splits, shuffle=True, random_state=random_state)
    param_grid = {"svm__C": [1.0, 10.0, 100.0], "svm__gamma": ["scale", "auto"]}

    gs = GridSearchCV(pipe, param_grid, scoring="f1_macro", cv=cv, n_jobs=-1, verbose=0)
    gs.fit(Xtr, ytr)

    best = gs.best_estimator_
    ypred = best.predict(Xva)
    acc = accuracy_score(yva, ypred)
    f1m = f1_score(yva, ypred, average="macro")

    logger.info(f"best_params: {gs.best_params_}")
    logger.info(f"acc={acc:.4f} | f1_macro={f1m:.4f}")
    logger.info(classification_report(yva, ypred, digits=4))

    meta = {
        "feature_dim": int(X.shape[1]),
        "use_pca": bool(use_pca),
        "pca_var": 0.95 if use_pca else None,
        "cv_splits": int(n_splits),
        "metrics": {"val_accuracy": float(acc), "val_f1_macro": float(f1m)},
        "best_params": gs.best_params_,
        "num_samples": int(len(y)),
        "class_counts": {int(c): int((y == c).sum()) for c in np.unique(y)},
    }
    return best, meta
