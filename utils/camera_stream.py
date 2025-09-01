# unified_stream.py
# coding: utf-8
import time, queue, threading, random, ctypes, platform
from typing import Optional, Tuple, Union
import numpy as np

# OpenCV é comum aos dois backends
import cv2

try:
    cv2.setNumThreads(1)  # ajuda no jitter em máquinas fracas
except Exception:
    pass

# mvsdk é opcional; só importaremos quando necessário
_HAS_MVSDK = False
try:
    import mvsdk  # type: ignore

    _HAS_MVSDK = True
except Exception:
    _HAS_MVSDK = False


class _BaseBackend:
    """Contrato mínimo para backends de captura."""

    name: str = "Unknown"
    is_connected: bool = False

    def connect(self) -> bool:
        raise NotImplementedError

    def grab(self) -> Optional[np.ndarray]:
        """Retorna um frame np.ndarray (BGR ou MONO8) ou None se timeout/sem frame."""
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


# -----------------------
# Backend OpenCV (video/rtsp/usb genérico)
# -----------------------
class _OpenCVBackend(_BaseBackend):
    def __init__(self, source: Union[str, int]):
        super().__init__()
        self.source = source
        self.cap: Optional[cv2.VideoCapture] = None
        self.name = f"OpenCV:{source}"

    def connect(self) -> bool:
        self.close()
        cap = cv2.VideoCapture(self.source)
        if not cap or not cap.isOpened():
            self.is_connected = False
            return False

        # Best-effort: nem todos os backends respeitam
        try:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:
            pass
        try:
            cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 3000)
            cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, 3000)
        except Exception:
            pass

        self.cap = cap
        self.is_connected = True
        return True

    def grab(self) -> Optional[np.ndarray]:
        if not self.cap:
            return None
        ok, frame = self.cap.read()
        if not ok or frame is None:
            # sinalizar perda de conexão; quem chama decide reconectar
            self.is_connected = False
            return None
        return frame  # já é ndarray

    def close(self) -> None:
        try:
            if self.cap is not None:
                self.cap.release()
        except Exception:
            pass
        finally:
            self.cap = None
            self.is_connected = False


# -----------------------
# Backend mvsdk (câmera industrial USB)
# -----------------------
class _MvSdkBackend(_BaseBackend):
    def __init__(
        self,
        force_mono: bool = False,
        exposure_us: Optional[int] = 30000,
        resolution: Optional[Tuple[int, int]] = None,  # (w,h)
    ):
        if not _HAS_MVSDK:
            raise RuntimeError("mvsdk não disponível: verifique instalação do SDK.")
        super().__init__()
        self.force_mono = force_mono
        self.exposure_us = exposure_us
        self.resolution = resolution

        # handles mvsdk
        self.h = None
        self.cap = None
        self.is_mono = False
        self.p_frame = None
        self._buf_size = 0
        self.name = "MV-USB"

    def connect(self) -> bool:
        self.close()
        try:
            devs = mvsdk.CameraEnumerateDevice()
            if not devs:
                self.is_connected = False
                return False

            dev = devs[0]
            self.name = dev.GetFriendlyName()

            self.h = mvsdk.CameraInit(dev, -1, -1)
            self.cap = mvsdk.CameraGetCapability(self.h)
            hw_mono = self.cap.sIspCapacity.bMonoSensor != 0
            self.is_mono = bool(self.force_mono or hw_mono)

            # Formato de saída no ISP (ideal pro OpenCV)
            if self.is_mono:
                mvsdk.CameraSetIspOutFormat(self.h, mvsdk.CAMERA_MEDIA_TYPE_MONO8)
            else:
                mvsdk.CameraSetIspOutFormat(self.h, mvsdk.CAMERA_MEDIA_TYPE_BGR8)

            # Contínuo, exposição manual simples
            mvsdk.CameraSetTriggerMode(self.h, 0)
            mvsdk.CameraSetAeState(self.h, 0)
            if self.exposure_us is not None:
                mvsdk.CameraSetExposureTime(self.h, int(self.exposure_us))

            # (Opcional) reduzir resolução na própria câmera
            if self.resolution:
                w, h = self.resolution
                res = mvsdk.tSdkImageResolution()
                mvsdk.CameraGetImageResolution(self.h, res)
                res.iWidth, res.iHeight = int(w), int(h)
                mvsdk.CameraSetImageResolution(self.h, res)

            mvsdk.CameraPlay(self.h)

            # buffer alinhado do tamanho máximo possível
            max_w = self.cap.sResolutionRange.iWidthMax
            max_h = self.cap.sResolutionRange.iHeightMax
            channels = 1 if self.is_mono else 3
            self._buf_size = max_w * max_h * channels
            self.p_frame = mvsdk.CameraAlignMalloc(self._buf_size, 16)

            self.is_connected = True
            return True

        except mvsdk.CameraException:
            self.close()
            return False
        except Exception:
            self.close()
            return False

    def grab(self) -> Optional[np.ndarray]:
        if not self.h or not self.p_frame:
            return None
        try:
            # timeout curto evita travar se não houver frame
            p_raw, head = mvsdk.CameraGetImageBuffer(self.h, 200)
            mvsdk.CameraImageProcess(self.h, p_raw, self.p_frame, head)
            mvsdk.CameraReleaseImageBuffer(self.h, p_raw)

            if platform.system() == "Windows":
                mvsdk.CameraFlipFrameBuffer(self.p_frame, head, 1)

            size = head.uBytes
            cbuf = (ctypes.c_ubyte * size).from_address(self.p_frame)
            arr = np.frombuffer(cbuf, dtype=np.uint8)

            if head.uiMediaType == mvsdk.CAMERA_MEDIA_TYPE_MONO8:
                frame = arr.reshape(head.iHeight, head.iWidth).copy()
            else:
                frame = arr.reshape(head.iHeight, head.iWidth, 3).copy()

            return frame

        except mvsdk.CameraException as e:
            if e.error_code == mvsdk.CAMERA_STATUS_TIME_OUT:
                # sem frame no período; devolve None para o chamador decidir
                return None
            # erro real → marcar desconectado para reconexão
            self.is_connected = False
            return None
        except Exception:
            self.is_connected = False
            return None

    def close(self) -> None:
        try:
            if self.h is not None:
                try:
                    mvsdk.CameraUnInit(self.h)
                except Exception:
                    pass
        finally:
            self.h = None
            self.cap = None
            self.is_connected = False
        try:
            if self.p_frame is not None:
                mvsdk.CameraAlignFree(self.p_frame)
        except Exception:
            pass
        finally:
            self.p_frame = None


# -----------------------
# Classe unificada com flag de backend
# -----------------------
class BufferedVideoStream(threading.Thread):
    """
    backend:
      - "opencv": usa cv2.VideoCapture em `source` (arquivo, rtsp, índice USB genérico)
      - "mvsdk": usa SDK da câmera industrial (ignora `source`)
    """

    def __init__(
        self,
        backend: str,
        source: Union[str, int] = 0,
        buffer_size: int = 1,
        max_retries: Optional[int] = None,  # None = ilimitado
        reconnect_backoff: Tuple[float, float] = (0.5, 8.0),
        start_paused: bool = True,
        # opções específicas do mvsdk:
        mv_force_mono: bool = False,
        mv_exposure_us: Optional[int] = 30000,
        mv_resolution: Optional[Tuple[int, int]] = None,
    ):
        super().__init__(daemon=True)
        self.backend_name = backend.lower().strip()
        if self.backend_name == "opencv":
            self.backend = _OpenCVBackend(source)
        elif self.backend_name == "mvsdk":
            self.backend = _MvSdkBackend(
                force_mono=mv_force_mono,
                exposure_us=mv_exposure_us,
                resolution=mv_resolution,
            )
        else:
            raise ValueError("backend deve ser 'opencv' ou 'mvsdk'")

        self.buffer_size = buffer_size
        self.max_retries = max_retries
        self.reconnect_backoff = reconnect_backoff

        self.frame_buffer = queue.Queue(maxsize=buffer_size)
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._pause_event.set() if start_paused else self._pause_event.clear()

        self._last_frame_lock = threading.Lock()
        self.last_frame: Optional[np.ndarray] = None
        self.last_frame_ts: float = 0.0

        self._retry_count = 0

    # ===== infra =====
    def _sleep_with_backoff(self, mini: bool = False):
        lo, hi = self.reconnect_backoff
        base = lo if mini else min(hi, lo * (2 ** max(0, self._retry_count - 1)))
        delay = base + random.random() * 0.3  # jitter
        time.sleep(delay)

    def _connect(self) -> bool:
        ok = self.backend.connect()
        if ok:
            self._retry_count = 0
        return ok

    def _safe_release(self):
        self.backend.close()

    # ===== loop =====
    def run(self):
        while not self._stop_event.is_set():
            if self._pause_event.is_set():
                time.sleep(0.1)
                continue

            if not self.backend.is_connected:
                if not self._connect():
                    self._retry_count += 1
                    if (
                        self.max_retries is not None
                        and self._retry_count >= self.max_retries
                    ):
                        break
                    self._sleep_with_backoff()
                    continue

            frame = self.backend.grab()
            if frame is None:
                # timeout/erro → tentar reconectar (quando backend sinaliza desconectado)
                if not self.backend.is_connected:
                    self._retry_count += 1
                    self._safe_release()
                    if (
                        self.max_retries is not None
                        and self._retry_count >= self.max_retries
                    ):
                        break
                    self._sleep_with_backoff(mini=True)
                else:
                    # apenas faltou frame no intervalo; yield rápido
                    time.sleep(0.001)
                continue

            # push (drop oldest)
            if self.frame_buffer.full():
                try:
                    self.frame_buffer.get_nowait()
                except queue.Empty:
                    pass
            try:
                self.frame_buffer.put_nowait(frame)
            except queue.Full:
                pass

            with self._last_frame_lock:
                self.last_frame = frame
                self.last_frame_ts = time.time()

        self._cleanup()

    # ===== API pública (compatível) =====
    def read(self, copy: bool = False, timeout: float = 0.0) -> Optional[np.ndarray]:
        try:
            frame = (
                self.frame_buffer.get(timeout=timeout)
                if timeout and timeout > 0
                else self.frame_buffer.get_nowait()
            )
        except queue.Empty:
            with self._last_frame_lock:
                frame = self.last_frame

        if frame is None:
            return None
        return frame.copy() if copy else frame

    def pause(self):
        self._pause_event.set()
        with self._last_frame_lock:
            self.last_frame = None
            self.last_frame_ts = 0.0
        try:
            while not self.frame_buffer.empty():
                self.frame_buffer.get_nowait()
        except Exception:
            pass

    def resume(self):
        self._pause_event.clear()

    def stop(self):
        self._stop_event.set()
        self._safe_release()
        self.join(timeout=2.0)
        self._cleanup()

    def _cleanup(self):
        self._safe_release()
        try:
            while not self.frame_buffer.empty():
                self.frame_buffer.get_nowait()
        except Exception:
            pass

    def is_reading(self) -> bool:
        return self.is_alive() and not self._pause_event.is_set()

    def get_status(self) -> dict:
        return {
            "alive": self.is_alive(),
            "connected": bool(self.backend.is_connected),
            "paused": self._pause_event.is_set(),
            "retry_count": self._retry_count,
            "last_frame_age_ms": (
                int((time.time() - self.last_frame_ts) * 1000)
                if self.last_frame_ts
                else None
            ),
            "buffer_len": self.frame_buffer.qsize(),
            "device": getattr(self.backend, "name", self.backend_name),
            "backend": self.backend_name,
        }

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()
