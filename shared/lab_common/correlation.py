import os
import time
import uuid
from contextvars import ContextVar
from flask import request, g
from opentelemetry import trace

# Latência de processamento simulada por requisição (ms): cada serviço demora
# um pouco para atender, escalonando no tempo os logs do fluxo síncrono.
SERVICE_PROCESSING_LATENCY_MS = float(os.getenv('SERVICE_PROCESSING_LATENCY_MS', '10'))

# Correlation ID de negócio: começa no gateway (X-Correlation-ID) e é
# propagado através de chamadas HTTP (via lab_common.http_client) e mensagens.
_correlation_id: ContextVar = ContextVar('correlation_id', default=None)


def set_correlation_id(cid: str) -> None:
    _correlation_id.set(cid)


def get_correlation_id() -> str:
    return _correlation_id.get()


def correlation_middleware(app):
    """Lê (ou gera) o X-Correlation-ID da requisição e o ecoa na resposta.

    Use em qualquer serviço: `correlation_middleware(app)` após criar o Flask.
    """

    @app.before_request
    def _before_request():
        if SERVICE_PROCESSING_LATENCY_MS > 0:
            time.sleep(SERVICE_PROCESSING_LATENCY_MS / 1000.0)
        cid = (
            request.headers.get('X-Correlation-ID')
            or request.headers.get('X-Request-ID')
            or str(uuid.uuid4())
        )
        set_correlation_id(cid)
        g.correlation_id = cid
        span = trace.get_current_span()
        if span.get_span_context().is_valid:
            span.set_attribute("messaging.correlation_id", cid)

    @app.after_request
    def _after_request(resp):
        cid = get_correlation_id() or getattr(g, 'correlation_id', None)
        if cid:
            resp.headers['X-Correlation-ID'] = cid
        return resp