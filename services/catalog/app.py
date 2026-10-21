import uuid
from flask import Response
from apiflask import APIFlask
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST, Counter
from lab_common.logger import setup_logger
from lab_common.tracing import init_tracing
from lab_common.health import make_health_blueprint
from lab_common.correlation import correlation_middleware
from models import init_db, SessionLocal, Product, to_dict
from schemas import ProductIn, ProductUpdateIn, StockChangeIn
from sqlalchemy import text

REQUEST_COUNT = Counter('http_requests_total', 'Total HTTP requests', ['method', 'endpoint'])
PRODUCTS_CREATED = Counter('products_created_total', 'Products created')

SEED_PRODUCTS = [
    ('Notebook', 3500.00, 10, True),
    ('Mouse', 150.00, 50, False),
    ('Teclado', 250.00, 30, False),
    ('Monitor', 1200.00, 15, True),
    ('Headset', 400.00, 20, True),
]

app = APIFlask(__name__, title='Catalog Service', version='1.0.0')
logger = setup_logger('catalog')
init_tracing('catalog-service', app)
init_db()
correlation_middleware(app)


def seed_if_empty():
    """Seed idempotente: popula o catálogo na primeira subida."""
    s = SessionLocal()
    if s.query(Product).count() == 0:
        for name, price, stock, eligible in SEED_PRODUCTS:
            s.add(Product(name=name, price=price, stock=stock, extended_warranty_eligible=eligible))
        s.commit()
        logger.info('Catalog seeded with default products')
    s.close()


seed_if_empty()

def check_db():
    try:
        s = SessionLocal()
        s.execute(text('SELECT 1'))
        s.close()
        return True
    except Exception:
        return False

app.register_blueprint(make_health_blueprint(check_db=check_db))


@app.get('/products')
def list_products():
    REQUEST_COUNT.labels(method='GET', endpoint='/products').inc()
    logger.info('Catalog listed')
    s = SessionLocal()
    items = s.query(Product).order_by(Product.id).all()
    res = [to_dict(p) for p in items]
    s.close()
    return {'count': len(res), 'products': res}, 200


@app.get('/products/<int:pid>')
def get_product(pid):
    REQUEST_COUNT.labels(method='GET', endpoint='/products/:id').inc()
    logger.info('Product fetched', extra={'productId': pid})
    s = SessionLocal()
    p = s.query(Product).filter(Product.id == pid).first()
    if not p:
        s.close()
        return {'error': 'not found'}, 404
    r = to_dict(p)
    s.close()
    return r, 200


@app.post('/products')
@app.input(ProductIn)
def create_product(json_data):
    REQUEST_COUNT.labels(method='POST', endpoint='/products').inc()
    s = SessionLocal()
    p = Product(name=json_data.get('name'), price=json_data.get('price', 0), stock=json_data.get('stock', 0),
                extended_warranty_eligible=json_data.get('extendedWarrantyEligible', False))
    s.add(p)
    s.commit()
    s.refresh(p)
    r = to_dict(p)
    PRODUCTS_CREATED.inc()
    s.close()
    logger.info('Product created', extra={'productId': p.id})
    return r, 201


@app.put('/products/<int:pid>')
@app.input(ProductUpdateIn)
def update_product(pid, json_data):
    REQUEST_COUNT.labels(method='PUT', endpoint='/products/:id').inc()
    data = json_data
    s = SessionLocal()
    p = s.query(Product).filter(Product.id == pid).first()
    if not p:
        s.close()
        return {'error': 'not found'}, 404
    if 'name' in data and data['name'] is not None: p.name = data['name']
    if 'price' in data and data['price'] is not None: p.price = data['price']
    if 'stock' in data and data['stock'] is not None: p.stock = data['stock']
    if 'extendedWarrantyEligible' in data and data['extendedWarrantyEligible'] is not None: p.extended_warranty_eligible = data['extendedWarrantyEligible']
    s.commit()
    r = to_dict(p)
    s.close()
    return r, 200


@app.post('/products/<int:pid>/reserve')
@app.input(StockChangeIn)
def reserve_stock(pid, json_data):
    """Reserva estoque (saga): decrementa se houver quantidade disponível."""
    REQUEST_COUNT.labels(method='POST', endpoint='/products/:id/reserve').inc()
    qty = int(json_data.get('quantity', 1))
    s = SessionLocal()
    p = s.query(Product).filter(Product.id == pid).first()
    if not p:
        s.close()
        return {'error': 'not found'}, 404
    if p.stock < qty:
        s.close()
        return {'error': 'insufficient stock', 'stock': p.stock}, 409
    p.stock -= qty
    s.commit()
    logger.info('Stock reserved', extra={'productId': pid, 'quantity': qty, 'remaining': p.stock})
    r = to_dict(p)
    s.close()
    return r, 200


@app.post('/products/<int:pid>/release')
@app.input(StockChangeIn)
def release_stock(pid, json_data):
    """Devolve estoque (compensação): incrementa."""
    REQUEST_COUNT.labels(method='POST', endpoint='/products/:id/release').inc()
    qty = int(json_data.get('quantity', 1))
    s = SessionLocal()
    p = s.query(Product).filter(Product.id == pid).first()
    if not p:
        s.close()
        return {'error': 'not found'}, 404
    p.stock += qty
    s.commit()
    logger.info('Stock released', extra={'productId': pid, 'quantity': qty, 'remaining': p.stock})
    r = to_dict(p)
    s.close()
    return r, 200


@app.get('/metrics')
def metrics():
    return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
