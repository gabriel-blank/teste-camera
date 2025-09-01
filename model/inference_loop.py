# inference_loop.py (trecho atualizado)

import contextlib
from datetime import datetime, timezone
import io
import os
import time
import json
import threading
import warnings
import cv2
import torch
import numpy as np
import logging
from joblib import load
from anomalib.data.predict import PredictDataset
from anomalib.engine import Engine
from anomalib.models import Patchcore
from torch.utils.data import DataLoader
from torchvision.transforms.functional import to_tensor
from lightning.pytorch.callbacks import TQDMProgressBar

from utils.api_controller import ApiController
from utils.camera_stream import BufferedVideoStream
from utils.logger import logger
from utils.state_watcher import StateWatcher

# >>> extras: extrator de features compartilhado
from utils.feature_extractor import ResNetFeature, embed_region_from_frame_rgb

# 1) reduzir verbosidade do Lightning
os.environ["LIGHTNING_LOG_LEVEL"] = "ERROR"  # respeitado pelo lightning
for name in [
    "lightning",
    "pytorch_lightning",
    "lightning.pytorch",
    "lightning.pytorch.utilities.rank_zero",
]:
    lg = logging.getLogger(name)
    lg.setLevel(logging.CRITICAL)  # mais agressivo que ERROR
    lg.propagate = False

# 2) suprimir warnings do módulo lightning (inclui dicas de num_workers)
warnings.filterwarnings("ignore", module="lightning")

# helper p/ silenciar prints residuais
_silent_out = contextlib.redirect_stdout(io.StringIO())
_silent_err = contextlib.redirect_stderr(io.StringIO())


def _save_debug_artifacts(
    base_dir: str,
    po: int,
    frame_rgb: np.ndarray,
    poly_norm: list[list[float]],
    anomaly_map_t: torch.Tensor | None,
    anom_score: float | None,
    pred_class_id: int | None,
    pred_class_name: str | None,
    pred_confidence: float | None,
):
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S.%f")[:-3]
    out_dir = os.path.join(base_dir, f"PO_{po}", ts)
    os.makedirs(out_dir, exist_ok=True)

    H, W, _ = frame_rgb.shape

    cv2.imwrite(
        os.path.join(out_dir, "frame.jpg"), cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR)
    )

    overlay = frame_rgb.copy()
    if poly_norm:
        pts = np.array([[int(x * W), int(y * H)] for x, y in poly_norm], dtype=np.int32)
        cv2.polylines(overlay, [pts], isClosed=True, color=(255, 0, 0), thickness=2)

    y_cursor = 24

    def put_txt(txt):
        nonlocal y_cursor
        cv2.putText(
            overlay,
            txt,
            (8, y_cursor),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6,
            (255, 255, 255),
            2,
            cv2.LINE_AA,
        )
        y_cursor += 22

    if pred_class_name is not None and pred_confidence is not None:
        put_txt(f"class: {pred_class_id} - {pred_class_name} ({pred_confidence:.3f})")
    if anom_score is not None:
        put_txt(f"anom_score: {anom_score:.3f}")
    put_txt(f"PO: {po}  ts: {ts}")

    cv2.imwrite(
        os.path.join(out_dir, "overlay.jpg"), cv2.cvtColor(overlay, cv2.COLOR_RGB2BGR)
    )

    if anomaly_map_t is not None:
        amap = anomaly_map_t.detach().float().cpu().numpy()
        if amap.ndim == 3:
            amap = amap.squeeze(0)
        amap = np.clip(amap, 0.0, 1.0)
        amap_u8 = (amap * 255).astype(np.uint8)
        amap_u8 = cv2.resize(amap_u8, (W, H), interpolation=cv2.INTER_LINEAR)
        heat = cv2.applyColorMap(amap_u8, cv2.COLORMAP_JET)
        cv2.imwrite(os.path.join(out_dir, "heatmap.jpg"), heat)

    meta = {
        "timestamp": ts,
        "po": po,
        "polygon_norm": poly_norm,
        "anom_score": float(anom_score) if anom_score is not None else None,
        "pred_class_id": int(pred_class_id) if pred_class_id is not None else None,
        "pred_class_name": pred_class_name,
        "pred_confidence": (
            float(pred_confidence) if pred_confidence is not None else None
        ),
        "frame_shape": {"h": int(H), "w": int(W)},
    }
    with open(os.path.join(out_dir, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    return out_dir


def run_inference(
    cam_url: str,
    po: int,
    anomaly_ckpt_path: str,
    api: ApiController,
    svm_model_path: str,
    stop_event: threading.Event | None = None,
    svm_meta_path: str | None = None,
    detect_threshold: float = 0.8,
    save_debug: bool = True,
    debug_dir: str | None = "debug_runs",
):
    device = "cuda" if torch.cuda.is_available() else "cpu"
    if stop_event is None:
        stop_event = threading.Event()

    # ---------- Modelo / Engine (anomaly via Engine.predict) ----------
    with _silent_out, _silent_err:
        model = Patchcore.load_from_checkpoint(
            checkpoint_path=anomaly_ckpt_path, backbone="resnet50"
        )
    model.visualizer = None
    model = model.to(device).eval()
    engine = Engine(callbacks=[TQDMProgressBar(refresh_rate=0)], logger=False)

    # ---------- Classificador (SVM) ----------
    if svm_meta_path is None:
        svm_meta_path = os.path.join(
            os.path.dirname(svm_model_path) or ".", "model_meta.json"
        )
    try:
        clf = load(svm_model_path)  # pipeline: scaler -> (pca) -> svc
    except Exception as e:
        raise RuntimeError(f"Falha ao carregar SVM em '{svm_model_path}': {e}")

    class_map: dict[str, str] = {}
    try:
        with open(svm_meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
        class_map = meta.get("class_map", {}) or {}
    except Exception:
        logger.warning(
            f"Não foi possível carregar class_map em '{svm_meta_path}'. Usando nomes padrão."
        )

    extractor = ResNetFeature(device=device, use_half=True)
    try:
        extractor.warmup()
    except Exception:
        pass

    # ---------- Stream e estado ----------
    stream = BufferedVideoStream(cam_url, start_paused=True)
    watcher = StateWatcher(api, po, interval=3.0)
    stream.start()
    watcher.start()

    was_running = False
    THRESH = float(detect_threshold)
    logger.info("Iniciando loop")

    # função local de polígono (mesma da sua versão)
    def extract_anomaly_polygon(
        anomaly_map_t: torch.Tensor,
        frame_shape,
        tau: float = 0.6,
        tau_strong: float = 0.8,
        min_area_frac: float = 0.001,
        approx_eps_frac: float = 0.005,
        morph_kernel: int = 3,
        morph_iters: int = 1,
        min_box_frac: float = 0.06,
        min_box_px: int = 32,
    ) -> list[list[float]]:
        H, W = frame_shape[:2]

        def _norm(poly_px: np.ndarray) -> list[list[float]]:
            xs = poly_px[:, 0].astype(np.float32) / max(1, W)
            ys = poly_px[:, 1].astype(np.float32) / max(1, H)
            pts = np.stack([xs, ys], axis=1)
            pts = np.clip(pts, 0.0, 1.0)
            return [[float(x), float(y)] for x, y in pts]

        am = anomaly_map_t.detach().float().cpu().numpy()
        if am.ndim == 3:
            am = am.squeeze(0)
        am = np.clip(am, 0.0, 1.0).astype(np.float32)
        am_resized = cv2.resize(am, (W, H), interpolation=cv2.INTER_LINEAR)

        _, _, _, maxLoc = cv2.minMaxLoc(am_resized)

        thr_val = int(round(tau * 255))
        _, binary = cv2.threshold(
            (am_resized * 255).astype(np.uint8), thr_val, 255, cv2.THRESH_BINARY
        )
        if morph_kernel >= 3 and morph_kernel % 2 == 1 and morph_iters > 0:
            k = cv2.getStructuringElement(cv2.MORPH_RECT, (morph_kernel, morph_kernel))
            binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, k, iterations=morph_iters)
            binary = cv2.morphologyEx(
                binary, cv2.MORPH_CLOSE, k, iterations=morph_iters
            )

        cnts, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        area_min = W * H * float(min_area_frac)

        best = None
        for c in cnts or []:
            x, y, w, h = cv2.boundingRect(c)
            if (w * h) < area_min:
                continue
            mask = np.zeros((H, W), np.uint8)
            cv2.drawContours(mask, [c], -1, 255, -1)
            mean_val = cv2.mean(am_resized, mask=mask)[0]
            _, local_max, _, _ = cv2.minMaxLoc(am_resized, mask=mask)
            if local_max < tau_strong:
                continue
            contains_peak = cv2.pointPolygonTest(c, maxLoc, False) >= 0
            score = (1 if contains_peak else 0, float(mean_val))
            if (best is None) or (score > best[0]):
                best = (score, c)

        if best is None:
            px, py = int(maxLoc[0]), int(maxLoc[1])
            size = max(min_box_px, int(round(min(H, W) * min_box_frac)))
            w = h = size
            x1 = max(0, min(px - w // 2, W - 1))
            y1 = max(0, min(py - h // 2, H - 1))
            x2 = max(x1 + 1, min(x1 + w, W))
            y2 = max(y1 + 1, min(y1 + h, H))
            poly_px = np.array([[x1, y1], [x2, y1], [x2, y2], [x1, y2]], dtype=np.int32)
            return _norm(poly_px)

        c = best[1]
        peri = cv2.arcLength(c, True)
        eps = max(1.0, approx_eps_frac * peri)
        approx = cv2.approxPolyDP(c, eps, True)
        if approx is None or len(approx) < 3:
            x, y, w, h = cv2.boundingRect(c)
            poly_px = np.array(
                [[x, y], [x + w, y], [x + w, y + h], [x, y + h]], dtype=np.int32
            )
            return _norm(poly_px)

        poly_px = approx.reshape(-1, 2).astype(np.int32)
        poly_px[:, 0] = np.clip(poly_px[:, 0], 0, W - 1)
        poly_px[:, 1] = np.clip(poly_px[:, 1], 0, H - 1)
        return _norm(poly_px)

    try:
        while not stop_event.is_set():
            try:
                if watcher.get_state():
                    if not was_running:
                        stream.resume()
                        time.sleep(0.1)
                        was_running = True

                    t_total_0 = time.perf_counter()

                    # 1) leitura
                    t_read_0 = time.perf_counter()
                    frame = stream.read(timeout=0.2)
                    t_read_1 = time.perf_counter()
                    if frame is None:
                        # sempre logamos; sem frame, métricas abaixo vão refletir 0 nos passos seguintes
                        frame_rgb = None
                        predictions = []
                        poly_norm = None
                        pred_class_id = None
                        pred_class_name = None
                        pred_confidence = None
                        score = None
                        last_anomaly_map = None

                        # métricas “zeradas” (menos read_ms)
                        t_total_1 = time.perf_counter()
                        read_ms = (t_read_1 - t_read_0) * 1000.0
                        pre_ms = 0.0
                        anom_ms = 0.0
                        post_ms = 0.0
                        class_ms = 0.0
                        api_ms = 0.0
                        total_ms = (t_total_1 - t_total_0) * 1000.0

                        logger.info(
                            (
                                f"[PO {po}] | total={total_ms:.2f}ms read={read_ms:.2f}ms "
                                f"preprocess={pre_ms:.2f}ms anomaly_inf={anom_ms:.2f}ms "
                                f"post_process={post_ms:.2f}ms class_inf={class_ms:.2f}ms api={api_ms:.2f}ms"
                            )
                        )
                        continue

                    # 2) preprocess
                    t_pre_0 = time.perf_counter()
                    frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                    tensor = (to_tensor(frame_rgb) * 255).to(torch.uint8)
                    dataset = PredictDataset(images=[tensor])
                    loader = DataLoader(
                        dataset,
                        collate_fn=dataset.collate_fn,
                    )
                    t_pre_1 = time.perf_counter()

                    # 3) inferência (anomalia) via Engine
                    t_anom_0 = time.perf_counter()
                    with torch.inference_mode(), _silent_out, _silent_err:
                        predictions = engine.predict(
                            model=model, dataloaders=loader, return_predictions=True
                        )
                    t_anom_1 = time.perf_counter()

                    # 4+5) pós-processo + classificação (se houver detecção)
                    t_post_0 = time.perf_counter()
                    t_post_1 = t_post_0
                    t_class_0 = t_post_0
                    t_class_1 = t_post_0

                    pred_class_id = None
                    pred_class_name = None
                    pred_confidence = None
                    score = None
                    poly_norm = None
                    last_anomaly_map = None
                    api_ms = 0.0

                    for batch in predictions:
                        for sel_score, anomaly_map in zip(
                            batch.pred_score, batch.anomaly_map
                        ):
                            score = float(sel_score.detach().cpu().item())
                            if score < THRESH:
                                continue

                            t_post_0 = time.perf_counter()
                            poly_norm = extract_anomaly_polygon(
                                anomaly_map, frame.shape
                            )
                            last_anomaly_map = anomaly_map
                            t_post_1 = time.perf_counter()

                            # --- classificação SVM (opcional) ---
                            t_class_0 = time.perf_counter()
                            pred_class_name = None
                            pred_confidence = None
                            try:
                                emb = embed_region_from_frame_rgb(
                                    frame_rgb, poly_norm, extractor
                                )
                                proba = clf.predict_proba([emb])[0]
                                idx = int(np.argmax(proba))
                                cls_id = int(clf.classes_[idx])
                                cls_name = class_map.get(str(cls_id), f"class_{cls_id}")
                                conf = float(proba[idx])

                                # guardamos só o que vamos enviar
                                pred_class_name = cls_name
                                pred_confidence = conf
                            except Exception as e:
                                logger.error(
                                    f"[PO {po}] erro na classificação SVM: {e}"
                                )
                            t_class_1 = time.perf_counter()

                            # --- envio para a API ---
                            t_api_0 = time.perf_counter()
                            payload = {
                                "po": po,
                                "score_global": score,  # a API converte para anomalyScore
                                "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                                "polygon_norm": json.dumps(poly_norm),
                            }
                            # adiciona os novos campos apenas se existirem
                            if pred_class_name is not None:
                                payload["classPred"] = pred_class_name
                            if (pred_confidence is not None) and np.isfinite(
                                pred_confidence
                            ):
                                # pode mandar float direto; será lido como string no form e parseado no server
                                payload["predConf"] = float(pred_confidence)

                            ok, enc_jpg = cv2.imencode(
                                ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80]
                            )
                            if ok:
                                _ = api.send_frame(
                                    files={
                                        "imagem": (
                                            "frame.jpg",
                                            enc_jpg.tobytes(),
                                            "image/jpeg",
                                        )
                                    },
                                    data=payload,
                                )
                            t_api_1 = time.perf_counter()
                            api_ms = (t_api_1 - t_api_0) * 1000.0

                            if save_debug and debug_dir:
                                try:
                                    out_dir = _save_debug_artifacts(
                                        base_dir=debug_dir,
                                        po=po,
                                        frame_rgb=frame_rgb,
                                        poly_norm=poly_norm,
                                        anomaly_map_t=last_anomaly_map,
                                        anom_score=score,
                                        pred_class_id=pred_class_id,
                                        pred_class_name=pred_class_name,
                                        pred_confidence=pred_confidence,
                                    )
                                    logger.info(f"[PO {po}] debug salvo em: {out_dir}")
                                except Exception as ioe:
                                    logger.warning(
                                        f"[PO {po}] falha ao salvar debug: {ioe}"
                                    )

                            break
                        break

                    # ===== métricas (sempre logar) =====
                    t_total_1 = time.perf_counter()
                    read_ms = (t_read_1 - t_read_0) * 1000.0
                    pre_ms = (t_pre_1 - t_pre_0) * 1000.0
                    anom_ms = (t_anom_1 - t_anom_0) * 1000.0
                    post_ms = (
                        (t_post_1 - t_post_0) * 1000.0 if poly_norm is not None else 0.0
                    )
                    class_ms = (
                        (t_class_1 - t_class_0) * 1000.0
                        if pred_class_id is not None
                        else 0.0
                    )
                    total_ms = (t_total_1 - t_total_0) * 1000.0

                    logger.info(
                        (
                            f"[PO {po}] | total={total_ms:.2f}ms read={read_ms:.2f}ms "
                            f"preprocess={pre_ms:.2f}ms anomaly_inf={anom_ms:.2f}ms "
                            f"post_process={post_ms:.2f}ms class_inf={class_ms:.2f}ms api={api_ms:.2f}ms"
                        )
                    )

                    if pred_class_id is not None:
                        logger.info(
                            f"[PO {po}] classificação: id={pred_class_id} "
                            f"name={pred_class_name} conf={pred_confidence:.3f} "
                            f"(anom_score={score:.3f})"
                        )

                else:
                    if was_running:
                        stream.pause()
                        was_running = False
                    time.sleep(0.1)

            except Exception as e:
                logger.error(f"[PO {po}] erro no loop: {e}")
                time.sleep(0.5)

    finally:
        try:
            stream.pause()
        except Exception:
            pass
        try:
            stream.stop()
        except Exception:
            pass
        try:
            watcher.stop()
        except Exception:
            pass
        try:
            extractor.close()
        except Exception:
            pass
        logger.info(f"[PO {po}] Loop finalizado.")
