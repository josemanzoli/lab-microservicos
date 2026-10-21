import os
import json
import uuid
import time
import threading
from apiflask import APIFlask
from flask import request, Response
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST, Counter
from lab_common.logger import setup_logger
from lab_common.tracing import init_tracing, get_tracer
from lab_common.health import make_health_blueprint
from lab_common.http_client import HttpClient
from lab_common.correlation import correlation_middleware, get_correlation_id, set_correlation_id
from opentelemetry import propagate
from models import init_db, SessionLocal, Order, OutboxEvent
from schemas import CreateOrderIn
from sqlalchemy import text
from datetime import datetime, timezone
from lab_common.rabbitmq import rabbitmq_service

app = APIFlask(__name__, title='Order Service', version='1.0.0')
logger = setup_logger('order')
init_tracing('order-service', app)
init_db()
correlation_middleware(app)
hc = HttpClient()

CART_URL = os.getenv('CART_URL', 'http://cart:5000')
PAYMENT_URL = os.getenv('PAYMENT_URL', 'http://payment:5000')
CATALOG_URL = os.getenv('CATALOG_URL', 'http://catalog:5000')
EXCHANGE = 'orders.events'

REQUEST_COUNT = Counter('http_requests_total', 'HTTP', ['method', 'endpoint'])
ORDERS_CREATED = Counter('orders_created_total', 'Orders created', ['status'])
OUTBOX_PUBLISHED = Counter('outbox_events_published_total', 'Outbox events published')

try:
    rabbitmq_service.connect()
    rabbitmq_service.declare_topic_exchange(EXCHANGE, [])
except Exception:
    pass


def check_db():
    try:
        s = SessionLocal(); s.execute(text('SELECT 1')); s.close(); return True
    except Exception: return False


def check_rabbit():
    """RabbitMQ faz parte da dependência: o outbox publica eventos nele."""
    if not rabbitmq_service.is_connected():
        try:
            rabbitmq_service.connect()
        except Exception:
            return False
    return rabbitmq_service.is_connected()


app.register_blueprint(make_health_blueprint(check_db=check_db, check_rabbit=check_rabbit))


def _append_outbox(s, aggregate_id, event_type, routing_key, payload):
    # Congela o W3C Trace Context da requisição dentro do evento: como a publicação
    # acontece depois (outra thread), é isso que permite ao relay continuar o MESMO
    # trace original (traceparent/tracestate) lá na frente.
    propagate.inject(payload)
    s.add(OutboxEvent(
        event_id=str(uuid.uuid4()),
        aggregate_id=aggregate_id,
        event_type=event_type,
        routing_key=routing_key,
        payload=json.dumps(payload),
    ))


def _reserve_stock(items):
    """Reserva estoque no catálogo (saga). Retorna erro se não há estoque."""
    reserved = []
    for it in items:
        pid = it.get('productId')
        qty = it.get('quantity', 1)
        try:
            r = hc.call(f'{CATALOG_URL}/products/{pid}/reserve', method='POST',
                        json={'quantity': qty}, timeout=2)
            if r.status_code >= 400:
                raise Exception(f'insufficient stock for {pid}')
            reserved.append((pid, qty))
        except Exception as e:
            # compensa o que já foi reservado
            for (rpid, rqty) in reserved:
                try:
                    hc.call(f'{CATALOG_URL}/products/{rpid}/release', method='POST',
                            json={'quantity': rqty}, timeout=2)
                except Exception:
                    pass
            raise Exception(f'reserve failed: {e}')
    return reserved


def _release_stock(items):
    """Compensa: devolve estoque reservado (best-effort)."""
    for it in items:
        pid = it.get('productId')
        qty = it.get('quantity', 1)
        try:
            hc.call(f'{CATALOG_URL}/products/{pid}/release', method='POST',
                    json={'quantity': qty}, timeout=2)
        except Exception as e:
            logger.error(f'release stock failed', extra={'productId': pid, 'error': str(e)})


def outbox_relay(stop_event):
    """Loop em background: publica eventos do outbox (at-least-once)."""
    logger.info('Outbox relay started.')
    while not stop_event.is_set():
        try:
            s = SessionLocal()
            pending = s.query(OutboxEvent).filter(OutboxEvent.published == False).order_by(OutboxEvent.id).limit(20).all()
            for ev in pending:
                try:
                    payload = json.loads(ev.payload)
                    # Restaura no relay o trace context congelado no evento (thread da
                    # requisição já morreu): o span "outbox_publish" vira o pai do que
                    # for injetado na mensagem, e os workers continuam o trace original.
                    context = propagate.extract(payload)
                    correlation_id = payload.get('correlationId') or ev.aggregate_id
                    set_correlation_id(correlation_id)
                    with get_tracer().start_as_current_span("outbox_publish", context=context) as span:
                        span.set_attribute("messaging.correlation_id", correlation_id)
                        rabbitmq_service.publish(
                            payload,
                            correlation_id=correlation_id,
                            exchange=EXCHANGE,
                            routing_key=ev.routing_key,
                        )
                        ev.published = True
                        ev.published_at = datetime.now(timezone.utc)
                        s.commit()
                        OUTBOX_PUBLISHED.inc()
                        logger.info('Outbox event published', extra={'eventType': ev.event_type, 'aggregateId': ev.aggregate_id, 'correlationId': correlation_id})
                except Exception as e:
                    s.rollback()
                    logger.error(f'outbox publish failed (will retry): {e}', extra={'aggregateId': ev.aggregate_id})
                    break
            s.close()
        except Exception as e:
            logger.error(f'outbox relay error: {e}')
        time.sleep(1)


@app.post('/orders')
@app.input(CreateOrderIn)
def create_order(json_data):
    REQUEST_COUNT.labels(method='POST', endpoint='/orders').inc()
    cart_id = json_data.get('cartId')
    logger.info('Checkout started — reading cart', extra={'cartId': cart_id})
    try:
        r = hc.call(f'{CART_URL}/carts/{cart_id}', method='GET', timeout=2)
        cart = r.json()
    except Exception as e:
        return {'error': f'cart error: {e}'}, 503
    total = cart.get('total', 0)
    items = cart.get('items', [])
    order_id = str(uuid.uuid4())
    correlation_id = get_correlation_id()

    # 1. Reserva de estoque (antes do pagamento) — parte da saga
    logger.info('Reserving stock for order', extra={'orderId': order_id, 'items': len(items)})
    try:
        _reserve_stock(items)
    except Exception as e:
        return {'orderId': order_id, 'status': 'REJECTED', 'reason': str(e)}, 409

    s = SessionLocal()
    o = Order(order_id=order_id, cart_id=cart_id, total=total, status='PENDING')
    s.add(o)
    s.commit()

    # 2. Pagamento
    logger.info('Processing payment for order', extra={'orderId': order_id, 'amount': float(total)})
    try:
        r2 = hc.call(f'{PAYMENT_URL}/payments', method='POST',
                     json={'orderId': order_id, 'amount': int(total)}, timeout=3)
        pay = r2.json()
    except Exception as e:
        # pagamento falhou → compensa (devolve estoque) + evento order.rejected NO MESMO commit
        o.status = 'REJECTED'
        _release_stock(items)
        _append_outbox(s, order_id, 'order.rejected', 'order.rejected',
                       {'orderId': order_id, 'status': 'REJECTED', 'cartId': cart_id, 'correlationId': correlation_id})
        s.commit(); s.close()
        ORDERS_CREATED.labels(status='REJECTED').inc()
        logger.error('Payment call failed; stock released', extra={'orderId': order_id})
        return {'orderId': order_id, 'status': 'REJECTED', 'reason': str(e)}, 503

    if pay.get('status') == 'APPROVED':
        o.status = 'PAID'
        _append_outbox(s, order_id, 'order.paid', 'order.paid',
                       {'orderId': order_id, 'total': total, 'items': items, 'cartId': cart_id, 'correlationId': correlation_id})
        ORDERS_CREATED.labels(status='PAID').inc()
        logger.info('Order paid', extra={'orderId': order_id})
    else:
        o.status = 'REJECTED'
        _release_stock(items)
        _append_outbox(s, order_id, 'order.rejected', 'order.rejected',
                       {'orderId': order_id, 'status': 'REJECTED', 'cartId': cart_id, 'correlationId': correlation_id})
        ORDERS_CREATED.labels(status='REJECTED').inc()
        logger.info('Order rejected; stock released', extra={'orderId': order_id})

    # 3. Ordem + evento do outbox commitados atomicamente
    s.commit()
    status_res = o.status
    s.close()
    return {'orderId': order_id, 'status': status_res, 'total': float(total)}, 200


@app.get('/orders/<oid>')
def get_order(oid):
    REQUEST_COUNT.labels(method='GET', endpoint='/orders/:id').inc()
    s = SessionLocal()
    o = s.query(Order).filter(Order.order_id == oid).first()
    if not o: s.close(); return {'error': 'not found'}, 404
    r = {'orderId': o.order_id, 'cartId': o.cart_id, 'total': float(o.total), 'status': o.status}
    s.close(); return r, 200


@app.get('/metrics')
def metrics():
    return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)


if __name__ == '__main__':
    stop_event = threading.Event()
    relay = threading.Thread(target=outbox_relay, args=(stop_event,), daemon=True)
    relay.start()
    app.run(host='0.0.0.0', port=5000)