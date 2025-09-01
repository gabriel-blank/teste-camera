import cv2
import threading
import time
import queue
from typing import Optional, Union
import numpy as np
from utils.logger import logger
import random


class BufferedVideoStream(threading.Thread):
    def __init__(
        self,
        source: Union[str, int],
        buffer_size: int = 1,
        max_retries: Optional[int] = None,  # None = ilimitado
        reconnect_backoff: tuple[float, float] = (0.5, 8.0),  # min, max (s)
        start_paused: bool = True,  # ponto 1: inicia pausado
    ):
        super().__init__(daemon=True)
        self.source = source
        self.buffer_size = buffer_size
        self.max_retries = max_retries
        self.reconnect_backoff = reconnect_backoff

        self.frame_buffer = queue.Queue(maxsize=buffer_size)
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        if start_paused:
            self._pause_event.set()  # set = pausado
        else:
            self._pause_event.clear()  # clear = rodando

        self._last_frame_lock = threading.Lock()
        self.cap: Optional[cv2.VideoCapture] = None
        self.last_frame: Optional[np.ndarray] = None
        self.last_frame_ts: float = 0.0
        self.is_connected = False
        self._retry_count = 0

    def run(self):
        while not self._stop_event.is_set():
            if self._pause_event.is_set():
                time.sleep(0.1)
                continue

            try:
                if not self.is_connected:
                    if not self._connect():
                        self._retry_count += 1
                        if (
                            self.max_retries is not None
                            and self._retry_count >= self.max_retries
                        ):
                            logger.error(
                                f"[Stream] Falha ao conectar após {self._retry_count} tentativas. Encerrando thread."
                            )
                            break
                        self._sleep_with_backoff()
                        continue
                    self._retry_count = 0

                ret, frame = self.cap.read()
                if not ret or frame is None:
                    # Perdeu frame/stream: reconecta
                    self._retry_count += 1
                    self._safe_release()
                    self.is_connected = False
                    if (
                        self.max_retries is not None
                        and self._retry_count >= self.max_retries
                    ):
                        logger.error(
                            "[Stream] Falha na captura de frames. Encerrando thread."
                        )
                        break
                    self._sleep_with_backoff(mini=True)
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

            except Exception as e:
                logger.error(f"[Stream] Erro durante captura: {e}")
                self._retry_count += 1
                self._safe_release()
                self.is_connected = False
                if (
                    self.max_retries is not None
                    and self._retry_count >= self.max_retries
                ):
                    logger.error("[Stream] Erros repetidos. Encerrando thread.")
                    break
                self._sleep_with_backoff(mini=True)

        self._cleanup()

    def _sleep_with_backoff(self, mini: bool = False):
        lo, hi = self.reconnect_backoff
        base = lo if mini else min(hi, lo * (2 ** max(0, self._retry_count - 1)))
        delay = base + random.random() * 0.3  # jitter
        time.sleep(delay)

    def _connect(self) -> bool:
        try:
            self._safe_release()

            self.cap = cv2.VideoCapture(self.source)
            if not self.cap or not self.cap.isOpened():
                return False

            # Best-effort: algumas builds ignoram essas props (ver explicação no item 4)
            try:
                self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            except Exception:
                pass
            try:
                self.cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 3000)
                self.cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, 3000)
            except Exception:
                pass

            self.is_connected = True
            return True

        except Exception as e:
            logger.error(f"[Stream] Erro ao conectar com {self.source}: {e}")
            self._safe_release()
            self.is_connected = False
            return False

    def _safe_release(self):
        try:
            if self.cap is not None:
                self.cap.release()
        except Exception:
            pass
        finally:
            self.cap = None

    def read(self, copy: bool = False, timeout: float = 0.0) -> Optional[np.ndarray]:
        """
        Devolve o frame mais recente.
        - Se houver na fila, retorna; senão, retorna last_frame.
        - copy=True: devolve uma cópia para evitar data races se o consumidor alterar o array.
        - timeout>0: espera por novo frame até esse tempo (seg).
        """
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

    def stop(self):
        self._stop_event.set()
        # Ponto 6: soltar a câmera antes do join ajuda a destravar read() em vários backends
        self._safe_release()
        self.join(timeout=2.0)
        if self.is_alive():
            logger.warning("[Stream] Thread não finalizou no timeout.")
        self._cleanup()

    def _cleanup(self):
        self._safe_release()
        # esvazia fila
        try:
            while not self.frame_buffer.empty():
                self.frame_buffer.get_nowait()
        except Exception:
            pass

    def pause(self):
        """Pausa a captura (mantém a conexão ativa). Zera o frame atual (ponto 5)."""
        self._pause_event.set()
        with self._last_frame_lock:
            self.last_frame = None
            self.last_frame_ts = 0.0
        # também drena a fila
        try:
            while not self.frame_buffer.empty():
                self.frame_buffer.get_nowait()
        except Exception:
            pass

    def resume(self):
        """Retoma a captura."""
        self._pause_event.clear()

    def is_reading(self) -> bool:
        """True se a thread está viva e NÃO está pausada."""
        return self.is_alive() and not self._pause_event.is_set()

    def get_status(self) -> dict:
        """Status para debug/monitoramento (ponto 7)."""
        return {
            "alive": self.is_alive(),
            "connected": bool(self.is_connected),
            "paused": self._pause_event.is_set(),
            "retry_count": self._retry_count,
            "last_frame_age_ms": (
                int((time.time() - self.last_frame_ts) * 1000)
                if self.last_frame_ts
                else None
            ),
            "buffer_len": self.frame_buffer.qsize(),
        }

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()
