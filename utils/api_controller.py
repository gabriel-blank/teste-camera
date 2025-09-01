import time
from typing import Any, Optional, List, Dict
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from requests.exceptions import ConnectionError, ReadTimeout, Timeout, RequestException
from utils.logger import logger
import threading


class _FakeResponse:
    """Evita quebrar chamadores quando falha duro de rede 2x seguidas."""

    def __init__(
        self,
        status_code: int,
        text: str = "",
        json_obj: Any = None,
        headers: dict | None = None,
    ):
        self.status_code = status_code
        self.text = text
        self._json_obj = json_obj
        self.headers = headers or {}

    def json(self):
        if self._json_obj is not None:
            return self._json_obj
        raise ValueError("Sem JSON disponível")


class ApiController:
    def __init__(self, config: dict):
        for f in ["url", "login", "senha", "cliente"]:
            if not config.get(f):
                raise ValueError(f"Campo obrigatório '{f}' ausente ou vazio no config.")
        self.url = config["url"].rstrip("/")
        self.login = config["login"]
        self.password = config["senha"]
        self.client = config["cliente"]
        self.timeout = config.get("timeout", 5)

        self.session: Optional[requests.Session] = None
        self.token: Optional[str] = None
        self._auth_lock = threading.Lock()

        self._build_session()
        self.authenticate()

    # -------- infra --------
    def _build_session(self):
        s = requests.Session()
        s.headers.update(
            {
                "Connection": "keep-alive",
                "Accept": "application/json, */*;q=0.1",
                "User-Agent": "tnah-infer/1.0",
            }
        )
        retry_get = Retry(
            total=3,
            backoff_factor=0.2,
            status_forcelist=(502, 503, 504),
            allowed_methods=("GET", "HEAD", "OPTIONS"),
            raise_on_status=False,
            respect_retry_after_header=True,
        )
        adapter = HTTPAdapter(
            max_retries=retry_get, pool_connections=32, pool_maxsize=32
        )
        s.mount("http://", adapter)
        s.mount("https://", adapter)
        self.session = s

    def _reset_session(self):
        try:
            if self.session:
                self.session.close()
        except Exception:
            pass
        self._build_session()
        if self.token:
            self.session.headers.update({"Authorization": f"Bearer {self.token}"})

    def close(self):
        try:
            if self.session:
                self.session.close()
        except Exception:
            pass

    # -------- helpers --------
    @staticmethod
    def _is_success(resp) -> bool:
        try:
            return 200 <= int(getattr(resp, "status_code", 0)) < 300
        except Exception:
            return False

    # -------- auth --------
    def authenticate(self):
        with self._auth_lock:
            try:
                r = self.session.post(
                    f"{self.url}/token",
                    json={
                        "login": self.login,
                        "senha": self.password,
                        "idCliente": self.client,
                    },
                    timeout=self.timeout,
                )
            except (ConnectionError, ReadTimeout, Timeout, RequestException) as e:
                logger.error(f"Falha na autenticação (erro de rede): {e}")
                self.token = None
                return

            if r.status_code == 200:
                try:
                    self.token = r.json().get("message")
                except Exception:
                    self.token = None
                if self.token:
                    self.session.headers.update(
                        {"Authorization": f"Bearer {self.token}"}
                    )
                    logger.info("Autenticação realizada com sucesso.")
                else:
                    logger.error(f"Token ausente na resposta do /token: {r.text}")
            else:
                logger.error(f"Falha na autenticação ({r.status_code}): {r.text}")
                self.token = None

    # -------- wrapper central --------
    def _request(
        self, method: str, path: str, *, allow_retry_post: bool = False, **kwargs
    ):
        """
        Proteções:
         - 401 -> reautentica (com lock) e repete 1x
         - ConnectionError/Timeout -> reseta sessão e repete 1x
         - 429 -> respeita Retry-After (se houver) e repete 1x
         - 5xx no POST -> 1 tentativa extra se allow_retry_post=True
        Retorna requests.Response ou _FakeResponse(599) em falhas duras.
        """
        url = f"{self.url}{path}"
        send = getattr(self.session, method.lower())

        def _do_send():
            return send(url, timeout=self.timeout, **kwargs)

        # 1ª tentativa
        try:
            resp = _do_send()
        except (ConnectionError, ReadTimeout, Timeout) as e:
            logger.warning(
                f"Conexão falhou; resetando sessão e tentando novamente... ({e})"
            )
            self._reset_session()
            try:
                resp = _do_send()
            except (ConnectionError, ReadTimeout, Timeout) as e2:
                logger.error(f"Segunda falha de rede em {method} {path}: {e2}")
                return _FakeResponse(599, text=str(e2))

        # 401 -> reautentica e repete uma vez
        if resp.status_code == 401:
            logger.warning("401 recebido; reautenticando...")
            self.authenticate()
            try:
                resp = _do_send()
            except (ConnectionError, ReadTimeout, Timeout) as e:
                logger.error(f"Falha após reautenticar em {method} {path}: {e}")
                return _FakeResponse(599, text=str(e))

        # 429 -> honra Retry-After e tenta 1x
        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After")
            delay = 0
            try:
                if retry_after:
                    delay = int(retry_after)
            except Exception:
                delay = 1
            delay = min(max(delay, 1), 10)
            logger.warning(f"429 recebido; aguardando {delay}s e tentando novamente...")
            time.sleep(delay)
            try:
                resp = _do_send()
            except (ConnectionError, ReadTimeout, Timeout) as e:
                logger.error(f"Falha após 429 em {method} {path}: {e}")
                return _FakeResponse(599, text=str(e))

        # 5xx em POST -> tentar mais 1x se permitido
        if resp.status_code >= 500 and method.upper() == "POST" and allow_retry_post:
            logger.warning(
                f"{resp.status_code} no POST; tentando novamente 1x (não idempotente)."
            )
            try:
                resp = _do_send()
            except (ConnectionError, ReadTimeout, Timeout) as e:
                logger.error(f"Falha de rede ao repetir POST {path}: {e}")
                return _FakeResponse(599, text=str(e))

        return resp

    # -------- APIs públicas --------
    def post_event(self, payload: list[dict]) -> bool:
        r = self._request("POST", "/Coletor", json=payload)
        if self._is_success(r):
            logger.info(f"Evento enviado com sucesso: {getattr(r, 'text', '')}")
            return True
        logger.error(
            f"Falha ao enviar evento ({getattr(r, 'status_code', '???')}): {getattr(r, 'text', '')}"
        )
        return False

    def get_state(self, id_po: int) -> bool:
        r = self._request(
            "GET",
            "/PostosOperacoesInfos/GetDinamico",
            params={"idPostoOperacao": id_po, "idInfo": 1},
        )
        if self._is_success(r):
            try:
                data = r.json()
                if isinstance(data, list) and data:
                    return str(data[0].get("Valor")) == "6"
                logger.error(f"Resposta inesperada do get_state: {data}")
            except Exception as e:
                logger.exception(f"Falha ao interpretar estado: {e}")
        else:
            logger.error(
                f"Falha ao obter estado ({getattr(r, 'status_code', '???')}): {getattr(r, 'text', '')}"
            )
        return False

    def send_frame(self, files, data) -> bool:
        """
        Envia frame para /anomalias/upload.
        Sucesso = qualquer 2xx. Em caso de 201, loga o Location.
        """
        r = self._request(
            "POST", "/anomalias/upload", data=data, files=files, allow_retry_post=True
        )
        if self._is_success(r):
            # tenta logar JSON amigável
            try:
                j = r.json()
                loc = ""
                if getattr(r, "status_code", 0) == 201:
                    loc = f" (Location: {r.headers.get('Location', '')})"
                logger.info(f"Imagem enviada com sucesso [{r.status_code}]{loc}: {j}")
            except Exception:
                # fallback pro texto cru
                loc = ""
                if getattr(r, "status_code", 0) == 201:
                    loc = f" (Location: {r.headers.get('Location', '')})"
                logger.info(
                    f"Imagem enviada com sucesso [{r.status_code}]{loc}: {getattr(r, 'text', '')}"
                )
            return True
        logger.error(
            f"Falha ao enviar imagem ({getattr(r, 'status_code', '???')}): {getattr(r, 'text', '')}"
        )
        return False

    # --- paginação ---
    def list_images_page(
        self, po: int, folder: str, *, skip: int = 0, take: int = 100
    ) -> list[dict]:
        """
        Retorna UMA página (default igual à API).
        """
        r = self._request(
            "GET",
            "/anomalias/list",
            params={"po": po, "folder": folder, "skip": skip, "take": take},
        )
        if self._is_success(r):
            try:
                data = r.json()
                return data if isinstance(data, list) else []
            except Exception as e:
                logger.exception(f"Falha ao interpretar JSON do list_images_page: {e}")
        else:
            logger.error(
                f"Falha ao listar anomalias ({getattr(r, 'status_code', '???')}): {getattr(r, 'text', '')}"
            )
        return []

    def list_images(self, po: int, folder: str, *, take: int = 500) -> list[dict]:
        """
        Retorna TODAS as imagens (varre páginas) para manter compatibilidade com o comportamento antigo.
        'take' define o tamanho de página (máx. aceito pela API é 500).
        """
        all_items: List[Dict] = []
        skip = 0
        while True:
            page = self.list_images_page(po, folder, skip=skip, take=take)
            if not page:
                break
            all_items.extend(page)
            # se veio menos que 'take', acabou
            if len(page) < take:
                break
            skip += take
        return all_items
