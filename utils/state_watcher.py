import threading
import time
import random
from typing import Callable, Optional
from utils.api_controller import ApiController
from utils.logger import logger


class StateWatcher(threading.Thread):
    """
    Observa o estado de um PO via API, com backoff e métricas básicas.
    API compatível:
      - get_state()
      - stop()
      - context manager (__enter__/__exit__)

    Extras:
      - get_status() -> dict
      - on_change callback opcional
    """

    def __init__(
        self,
        api: ApiController,
        po: int,
        interval: float = 3.0,
        max_backoff: float = 30.0,
        on_change: Optional[Callable[[bool], None]] = None,
    ):
        super().__init__(daemon=True)
        self.api = api
        self.po = po
        self.interval = float(interval)
        self.max_backoff = float(max_backoff)
        self.on_change = on_change

        self._state: bool = False
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()

        self.last_update_ts: float = 0.0
        self.consecutive_failures: int = 0
        self.last_error: Optional[str] = None

    def run(self):
        # primeira tentativa imediata (sem esperar interval)
        self._poll_once(initial=True)

        while not self._stop_event.is_set():
            # espera pelo intervalo, mas pode ser interrompido por stop()
            self._wake_event.wait(self.interval)
            self._wake_event.clear()
            if self._stop_event.is_set():
                break
            self._poll_once()

    def _poll_once(self, initial: bool = False):
        try:
            new_state = self.api.get_state(
                self.po
            )  # deve ser rápida; use timeout na ApiController
            self._set_state(new_state)
            self.consecutive_failures = 0
            self.last_error = None
            self.last_update_ts = time.time()
        except Exception as e:
            self.consecutive_failures += 1
            self.last_error = str(e)
            logger.error(f"[Watcher-{self.po}] Erro ao consultar estado: {e}")

            # backoff exponencial com jitter, mas sem bloquear o stop()
            backoff = min(
                self.max_backoff, (2 ** (self.consecutive_failures - 1)) * 0.5
            )
            backoff += random.random() * 0.3
            # Usa wait para poder interromper de imediato no stop
            self._wake_event.wait(backoff)
            self._wake_event.clear()

    def _set_state(self, new_state: bool):
        changed = False
        with self._lock:
            if new_state != self._state:
                self._state = new_state
                changed = True
        if changed and self.on_change:
            try:
                self.on_change(new_state)
            except Exception as e:
                logger.error(f"[Watcher-{self.po}] Erro no on_change: {e}")

    def get_state(self) -> bool:
        with self._lock:
            return self._state

    def get_status(self) -> dict:
        """Telemetria útil p/ debug/monitoramento."""
        with self._lock:
            state = self._state
        return {
            "po": self.po,
            "state": state,
            "last_update_age_ms": (
                int((time.time() - self.last_update_ts) * 1000)
                if self.last_update_ts
                else None
            ),
            "consecutive_failures": self.consecutive_failures,
            "last_error": self.last_error,
            "alive": self.is_alive(),
        }

    def stop(self):
        self._stop_event.set()
        self._wake_event.set()  # acorda a thread se estiver aguardando
        self.join(timeout=2.0)

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()
