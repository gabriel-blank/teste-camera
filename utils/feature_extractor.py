# features.py
from __future__ import annotations

import io
import json
import os
import time
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np
import requests
import torch
from PIL import Image
from torchvision import models, transforms

# ----------------- Configs globais do backbone -----------------
BACKBONE_INPUT = 224  # lado para o preprocess da ResNet
FEATURE_LAYER = "layer4"
FEATURE_DIM = 2048

# ----------------- I/O helpers -----------------
_HTTP_TIMEOUT = 15
_RETRIES = 2


def resolve_image_url(image_url: str, base_url_prefix: str = "") -> str:
    if image_url.startswith(("http://", "https://")):
        return image_url
    if base_url_prefix.startswith(("http://", "https://")):
        return base_url_prefix.rstrip("/") + "/" + image_url.lstrip("/")
    return os.path.join(base_url_prefix, image_url.lstrip("/"))


def load_image(url_or_path: str) -> Image.Image:
    """Carrega imagem de URL (http/https) ou caminho local."""
    if url_or_path.startswith(("http://", "https://")):
        last_err = None
        for _ in range(_RETRIES + 1):
            try:
                resp = requests.get(url_or_path, timeout=_HTTP_TIMEOUT)
                resp.raise_for_status()
                return Image.open(io.BytesIO(resp.content)).convert("RGB")
            except Exception as e:
                last_err = e
                time.sleep(0.5)
        raise RuntimeError(f"Falha ao baixar {url_or_path}: {last_err}")
    return Image.open(url_or_path).convert("RGB")


# ----------------- Polígono -----------------
def normalize_polygon(poly) -> List[List[float]] | None:
    """
    Aceita:
      - flat: [x1,y1,x2,y2,...]
      - pares: [[x,y], ...]
    Retorna pares (x,y) em [0,1], exige >=3 pontos.
    """
    if poly is None:
        return None

    if isinstance(poly, str):
        try:
            poly = json.loads(poly)
        except Exception:
            return None

    pts: List[List[float]] = []

    # Flat
    if isinstance(poly, list) and poly and isinstance(poly[0], (int, float)):
        if len(poly) < 6 or len(poly) % 2 != 0:
            return None
        it = iter(poly)
        for x, y in zip(it, it):
            xi = float(x)
            yi = float(y)
            if not (np.isfinite(xi) and np.isfinite(yi)):
                return None
            pts.append([max(0.0, min(1.0, xi)), max(0.0, min(1.0, yi))])

    # Pares
    elif isinstance(poly, list) and poly and isinstance(poly[0], (list, tuple)):
        for p in poly:
            if len(p) < 2:
                return None
            xi = float(p[0])
            yi = float(p[1])
            if not (np.isfinite(xi) and np.isfinite(yi)):
                return None
            pts.append([max(0.0, min(1.0, xi)), max(0.0, min(1.0, yi))])
    else:
        return None

    return pts if len(pts) >= 3 else None


# ----------------- Máscaras no feature map (otimizada) -----------------
def polygon_to_mask_on_feature_map(
    poly_norm: List[List[float]],
    feat_h: int,
    feat_w: int,
    input_w: int = BACKBONE_INPUT,
    input_h: int = BACKBONE_INPUT,
) -> np.ndarray:
    """
    Converte poly (0..1) em máscara [feat_h, feat_w] de forma vetorizada:
    1) Escala para o grid do feature map diretamente (mais rápido).
    2) Preenche polígono com cv2.fillPoly.
    """
    # Escala para coordenadas do feature map
    poly_fm = np.array(
        [[x * feat_w, y * feat_h] for (x, y) in poly_norm],
        dtype=np.float32,
    ).reshape((-1, 1, 2))
    poly_fm_i = poly_fm.astype(np.int32)

    mask = np.zeros((feat_h, feat_w), dtype=np.uint8)
    cv2.fillPoly(mask, [poly_fm_i], 1)
    return mask


def dilate_mask(mask: np.ndarray, k: int = 1) -> np.ndarray:
    if k <= 0:
        return mask.astype(np.uint8)
    kernel = np.ones((max(1, 2 * k + 1), max(1, 2 * k + 1)), np.uint8)
    return cv2.dilate(mask.astype(np.uint8), kernel, iterations=1)


# ----------------- Extrator de Features -----------------
class ResNetFeature:
    """
    Hooka o layer4 da ResNet50 e expõe métodos para obter embeddings:
      - region_embedding(img_pil, poly_norm) -> (C,)
      - region_and_background_embeddings(img_pil, poly_norm, margin_cells) -> (C,), (C,)

    Extras:
      - use_half (CUDA): ativa autocast para reduzir latência/memória
      - warmup(): faz um forward de aquecimento
      - close(): remove o hook do modelo (bom para long-running apps)
    """

    def __init__(self, device: Optional[str] = None, use_half: bool = True):
        self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        self.use_half = bool(use_half and self.device.startswith("cuda"))

        model = models.resnet50(weights=models.ResNet50_Weights.IMAGENET1K_V2)
        model.eval().requires_grad_(False)
        self.model = model.to(self.device, non_blocking=True)

        # Buffer por-forward + handle do hook
        self._feat_buf: Optional[torch.Tensor] = None
        layer = getattr(self.model, FEATURE_LAYER)
        self._hook_handle = layer.register_forward_hook(self._hook_capture)

        self.preproc = transforms.Compose(
            [
                transforms.Resize((BACKBONE_INPUT, BACKBONE_INPUT)),
                transforms.ToTensor(),
                transforms.Normalize(
                    mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]
                ),
            ]
        )

    def _hook_capture(self, _module, _inp, out):
        # out: [B,C,Hf,Wf]; usamos apenas B=1
        # detach + contiguous para evitar guardar grafo
        self._feat_buf = out.detach()

    def close(self):
        """Remove hook para evitar leaks quando o objeto não for mais usado."""
        try:
            if self._hook_handle is not None:
                self._hook_handle.remove()
        except Exception:
            pass
        self._hook_handle = None

    def warmup(self):
        """Faz um forward rápido só para compilar/cuDNN autotune etc."""
        img = Image.new("RGB", (BACKBONE_INPUT, BACKBONE_INPUT), (0, 0, 0))
        _ = self._forward_and_get(img)

    def last_feat_shape(self) -> Optional[Tuple[int, int, int]]:
        t = self._feat_buf
        return tuple(t.shape[1:]) if t is not None else None  # (C,Hf,Wf)

    @torch.inference_mode()
    def _forward_and_get(self, img_pil: Image.Image) -> torch.Tensor:
        self._feat_buf = None
        x = (
            self.preproc(img_pil).unsqueeze(0).to(self.device, non_blocking=True)
        )  # [1,3,224,224]
        if self.use_half:
            # autocast fp16 em CUDA
            with torch.autocast(device_type="cuda", dtype=torch.float16):
                _ = self.model(x)
        else:
            _ = self.model(x)
        feat: torch.Tensor | None = self._feat_buf
        if feat is None:
            raise RuntimeError("Feature map não capturado; verifique o hook do layer4.")
        return feat.squeeze(0).contiguous()  # [C,Hf,Wf]

    @torch.inference_mode()
    def region_embedding(
        self, img_pil: Image.Image, poly_norm: List[List[float]]
    ) -> np.ndarray:
        feat = self._forward_and_get(img_pil)  # [C,Hf,Wf]
        _, Hf, Wf = feat.shape
        mask = polygon_to_mask_on_feature_map(poly_norm, feat_h=Hf, feat_w=Wf)
        mask_t = torch.from_numpy(mask).to(self.device).float()  # [Hf,Wf]

        if mask_t.sum() < 1.0:
            emb = feat.mean(dim=(1, 2))  # GAP fallback
        else:
            m = mask_t.unsqueeze(0)  # [1,Hf,Wf]
            emb = (feat * m).sum(dim=(1, 2)) / (m.sum() + 1e-6)

        return emb.detach().cpu().numpy()  # (C,)

    @torch.inference_mode()
    def region_and_background_embeddings(
        self,
        img_pil: Image.Image,
        poly_norm: List[List[float]],
        margin_cells: int = 1,
    ) -> Tuple[np.ndarray, np.ndarray, Dict[str, bool]]:
        feat = self._forward_and_get(img_pil)  # [C,Hf,Wf]
        _, Hf, Wf = feat.shape

        mask = polygon_to_mask_on_feature_map(poly_norm, feat_h=Hf, feat_w=Wf)
        bg_mask = 1 - dilate_mask(mask, k=margin_cells)

        mask_t = torch.from_numpy(mask).to(self.device).float()
        bg_t = torch.from_numpy(bg_mask).to(self.device).float()

        # poly
        if mask_t.sum() < 1.0:
            emb_poly = feat.mean(dim=(1, 2))
            poly_empty = True
        else:
            m = mask_t.unsqueeze(0)
            emb_poly = (feat * m).sum(dim=(1, 2)) / (m.sum() + 1e-6)
            poly_empty = False

        # background
        if bg_t.sum() < 1.0:
            emb_bg = feat.mean(dim=(1, 2))
            bg_empty = True
        else:
            b = bg_t.unsqueeze(0)
            emb_bg = (feat * b).sum(dim=(1, 2)) / (b.sum() + 1e-6)
            bg_empty = False

        return (
            emb_poly.detach().cpu().numpy(),
            emb_bg.detach().cpu().numpy(),
            {"poly_empty": poly_empty, "bg_empty": bg_empty},
        )


# ----------------- Conveniência (opcional) -----------------
def embed_region_from_frame_rgb(
    frame_rgb: np.ndarray, poly_norm: List[List[float]], extractor: ResNetFeature
) -> np.ndarray:
    """Atalho para inferência online: usa o frame RGB (np.uint8) já em memória."""
    img_pil = Image.fromarray(frame_rgb)
    return extractor.region_embedding(img_pil, poly_norm)
