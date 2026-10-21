import os
from apiflask import APIFlask
from flask import request, Response
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST, Counter
from lab_common.logger import setup_logger
from lab_common.tracing import init_tracing
from lab_common.health import make_health_blueprint
from lab_common.http_client import HttpClient
from lab_common.correlation import correlation_middleware
from models import init_db, SessionLocal, Cart, CartItem
from schemas import CartItemIn
from sqlalchemy import text

app = APIFlask(__name__, title='Cart Service', version='1.0.0')
logger = setup_logger('cart')
init_tracing('cart-service', app)
init_db()
correlation_middleware(app)
hc = HttpClient()

CATALOG_URL = os.getenv('CATALOG_URL', 'http://catalog:5000')

REQUEST_COUNT = Counter('http_requests_total', 'HTTP', ['method', 'endpoint'])

def check_db():
    try:
        s = SessionLocal(); s.execute(text('SELECT 1')); s.close(); return True
    except Exception: return False

app.register_blueprint(make_health_blueprint(check_db=check_db))


@app.get('/carts/<cid>')
def get_cart(cid):
    REQUEST_COUNT.labels(method='GET', endpoint='/carts/:id').inc()
    logger.info('Cart queried', extra={'cartId': cid})
    s = SessionLocal()
    items = s.query(CartItem).filter(CartItem.cart_id == cid).all()
    total = sum(float(i.price)*i.quantity for i in items)
    res = {'cartId': cid, 'items': [{'productId': i.product_id, 'name': i.name, 'price': float(i.price), 'quantity': i.quantity} for i in items], 'total': total}
    s.close(); return res, 200


@app.post('/carts/<cid>/items')
@app.input(CartItemIn)
def add_item(cid, json_data):
    REQUEST_COUNT.labels(method='POST', endpoint='/carts/:id/items').inc()
    pid = json_data.get('productId')
    qty = int(json_data.get('quantity', 1))
    logger.info('Validating product in catalog', extra={'cartId': cid, 'productId': pid})
    try:
        r = hc.call(f'{CATALOG_URL}/products/{pid}', method='GET', timeout=2)
        prod = r.json()
    except Exception as e:
        return {'error': f'catalog error: {e}'}, 503
    s = SessionLocal()
    ci = CartItem(cart_id=cid, product_id=pid, name=prod['name'], price=prod['price'], quantity=qty)
    s.add(ci); s.commit(); s.close()
    logger.info('Item added to cart', extra={'cartId': cid, 'productId': pid})
    return {'ok': True}, 201


@app.get('/metrics')
def metrics():
    return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
