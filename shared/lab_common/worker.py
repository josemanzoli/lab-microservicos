import os
import time
import json
import random
import sys
from opentelemetry import propagate
from prometheus_client import start_http_server, Counter
from .logger import setup_logger
from .rabbitmq import rabbitmq_service
from .correlation import set_correlation_id
from .tracing import init_tracing, get_tracer

# Delay simulado antes de começar a consumir a mensagem (ms): cria uma separação
# visível nos logs entre o "Outbox event published" (relay do order) e o
# "Message received" (worker), evidenciando o padrão outbox.
CONSUME_DELAY_MS = float(os.getenv('CONSUME_DELAY_MS', '10'))


def _make_callback(service_name, handler, logger, tracer, processed_cache,
                   PROCESSED, ERRORS, max_retries, initial_delay, simulate_fail_rate):
    def callback(ch, method, properties, body):
        if CONSUME_DELAY_MS > 0:
            time.sleep(CONSUME_DELAY_MS / 1000.0)
        start_time = time.time()
        context = propagate.extract(properties.headers or {})
        with tracer.start_as_current_span(f"rabbitmq_consume_{service_name}", context=context) as span:
            try:
                decoded = body.decode('utf-8')
                data = json.loads(decoded)
                correlation_id = properties.correlation_id or data.get('correlationId') or data.get('correlation_id') or data.get('orderId')
                span.set_attribute("messaging.correlation_id", correlation_id or "unknown")
                # Propaga o ID de negócio para chamadas HTTP que o handler fizer
                # (ex.: warranty → catalog), mantendo TUDO no mesmo correlationId.
                set_correlation_id(correlation_id)

                if correlation_id in processed_cache:
                    logger.warning("Idempotency: duplicate dropped.", extra={"correlationId": correlation_id})
                    ch.basic_ack(delivery_tag=method.delivery_tag)
                    return

                logger.info("Message received.", extra={"correlationId": correlation_id})
                retry_count = 0
                delay = initial_delay
                while retry_count <= max_retries:
                    try:
                        if simulate_fail_rate > 0 and random.random() < simulate_fail_rate:
                            raise ValueError("Simulated failure")
                        handler(data, correlation_id, span)
                        processed_cache.add(correlation_id)
                        PROCESSED.inc()
                        ch.basic_ack(delivery_tag=method.delivery_tag)
                        logger.info("Processed successfully.", extra={"correlationId": correlation_id})
                        return
                    except Exception as e:
                        retry_count += 1
                        if retry_count > max_retries:
                            logger.error(f"Failed after {max_retries} retries -> DLQ", extra={"correlationId": correlation_id, "error": str(e)})
                            ERRORS.inc()
                            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
                            return
                        logger.warning(f"Retry {retry_count}/{max_retries} in {delay}s", extra={"correlationId": correlation_id})
                        time.sleep(delay)
                        delay *= 2
            except Exception as e:
                logger.error(f"Unexpected error: {str(e)}")
                ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
    return callback


def run_workers(service_name, bindings, metrics_port=8000, max_retries=3, initial_delay=0.5,
                simulate_fail_rate=0.0):
    """Consome múltiplas filas. `bindings` é uma lista de (queue_name, handler).

    Idempotência: o cache em memória deduplica em execução; a durabilidade real
    (sobreviver a restart/replica) deve vir de constraint UNIQUE + upsert no
    handler de cada serviço.
    """
    logger = setup_logger(f"{service_name}-logger")
    init_tracing(f"{service_name}-worker")
    tracer = get_tracer()

    PROCESSED = Counter(f'{service_name}_processed_total', f'Total {service_name} processed')
    ERRORS = Counter(f'{service_name}_errors_total', f'Total {service_name} errors')
    processed_cache = set()

    try:
        start_http_server(metrics_port)
        logger.info(f"Metrics on :{metrics_port}")
        rabbitmq_service.connect()
        ch = rabbitmq_service.channel
        ch.basic_qos(prefetch_count=1)
        for queue, handler in bindings:
            cb = _make_callback(service_name, handler, logger, tracer, processed_cache,
                                PROCESSED, ERRORS, max_retries, initial_delay, simulate_fail_rate)
            ch.basic_consume(queue=queue, on_message_callback=cb, auto_ack=False)
        logger.info("[*] Waiting for messages")
        ch.start_consuming()
    except KeyboardInterrupt:
        rabbitmq_service.close()
        sys.exit(0)


def run_worker(service_name, queue_name, handler, metrics_port=8000, max_retries=3,
               initial_delay=0.5, simulate_fail_rate=0.0):
    """Compat: consume uma única fila."""
    run_workers(service_name, [(queue_name, handler)], metrics_port, max_retries,
                initial_delay, simulate_fail_rate)