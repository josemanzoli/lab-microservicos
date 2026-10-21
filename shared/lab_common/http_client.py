import os
import time
from urllib.parse import urlparse
import pybreaker
import requests
from opentelemetry import propagate
from .correlation import get_correlation_id

# Latência de rede simulada entre serviços (ms) — espalha no tempo os logs do
# fluxo síncrono (gateway → order → cart → catalog → payment), refletindo a
# ordem real de um pedido em produção.
HTTP_CALL_LATENCY_MS = float(os.getenv('HTTP_CALL_LATENCY_MS', '20'))


class HttpClient:
    """Cliente HTTP com Circuit Breaker persistente por downstream e
    propagação de contexto W3C (distributed tracing) + X-Correlation-ID.
    """

    def __init__(self):
        # Um Circuit Breaker por serviço downstream (não por chamada),
        # caso contrário o fail_max nunca acumula e o circuito nunca abre.
        self._breakers = {}

    def _breaker_for(self, url: str, fail_max: int, reset_timeout: int) -> pybreaker.CircuitBreaker:
        # Usa o host como identidade do downstream (ex.: "payment:5000")
        host = urlparse(url).netloc or url
        key = host
        if key not in self._breakers:
            self._breakers[key] = pybreaker.CircuitBreaker(
                fail_max=fail_max,
                reset_timeout=reset_timeout,
                name=f"http:{host}",
            )
        return self._breakers[key]

    def call(self, url: str, method: str = 'GET', json: dict = None,
             timeout: int = 2, fail_max: int = 3, reset_timeout: int = 30):
        breaker = self._breaker_for(url, fail_max, reset_timeout)

        @breaker
        def _do():
            if HTTP_CALL_LATENCY_MS > 0:
                time.sleep(HTTP_CALL_LATENCY_MS / 1000.0)
            headers = {}
            # Injeta o contexto de tracing (W3C Trace Context) nos headers,
            # permitindo que o trace atravesse as chamadas HTTP entre serviços.
            propagate.inject(headers)
            # Propaga o correlation id de negócio quando existir
            cid = get_correlation_id()
            if cid:
                headers['X-Correlation-ID'] = cid
            r = requests.request(method, url, json=json, timeout=timeout, headers=headers)
            if r.status_code >= 500:
                raise Exception(f"HTTP {r.status_code}")
            return r

        return _do()

    def breaker_state(self, url: str) -> str:
        return str(self._breaker_for(url, 3, 30).current_state)