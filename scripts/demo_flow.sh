#!/usr/bin/env bash
# Demonstra o fluxo completo (happy path): catálogo → carrinho → pedido → NF/logística/garantia.
set -euo pipefail

GW="http://localhost:8080"
CART_ID="cart-$(date +%s)"

echo "==> 1. Health do gateway"
curl -s "$GW/health"; echo

echo "==> 2. Catálogo (3 primeiros produtos)"
curl -s "$GW/api/v1/catalogo/products" | python3 -c "import sys,json; d=json.load(sys.stdin); [print(f'  id={p[\"id\"]} {p[\"name\"]} R\${p[\"price\"]} stock={p[\"stock\"]} warranty={p[\"extendedWarrantyEligible\"]}') for p in d['products'][:3]]"

echo "==> 3. Adiciona Notebook (id=1, elegível a garantia) ao carrinho"
curl -s -X POST "$GW/api/v1/carrinho/carts/$CART_ID/items" -H "Content-Type: application/json" -d '{"productId":1,"quantity":1}'; echo

echo "==> 4. Cria o pedido (checkout + pagamento)"
ORDER=$(curl -s -X POST "$GW/api/v1/pedidos/orders" -H "Content-Type: application/json" -d "{\"cartId\":\"$CART_ID\"}")
echo "  $ORDER"
ORDER_ID=$(echo "$ORDER" | python3 -c "import sys,json; print(json.load(sys.stdin).get('orderId',''))")
STATUS=$(echo "$ORDER" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status',''))")
REASON=$(echo "$ORDER" | python3 -c "import sys,json; print(json.load(sys.stdin).get('reason',''))")

if [ "$STATUS" = "PAID" ]; then
  echo "==> 5. Aguardando workers assíncronos (NF, logística, garantia)..."
  sleep 8
  echo "  NF emitida:"
  docker compose exec -T postgres psql -U admin -d invoice_db -c "SELECT order_id, total FROM invoices WHERE order_id='$ORDER_ID';" 2>/dev/null || true
  echo "  Entrega:"
  docker compose exec -T postgres psql -U admin -d logistics_db -c "SELECT order_id, status FROM deliveries WHERE order_id='$ORDER_ID';" 2>/dev/null || true
  echo "  Garantia estendida:"
  docker compose exec -T postgres psql -U admin -d warranty_db -c "SELECT order_id, product_id, extended FROM warranties WHERE order_id='$ORDER_ID';" 2>/dev/null || true
  echo "  Estoque do produto 1 (deve ter decrementado):"
  docker compose exec -T postgres psql -U admin -d catalog_db -c "SELECT id, stock FROM products WHERE id=1;" 2>/dev/null || true
else
  echo "  Pedido REJECTED — motivo: $REASON"
  echo "  Dica: se for estoque, reponha via PUT /api/v1/catalogo/products/<id>; se for pagamento, ajuste PAYMENT_FAILURE_RATE."
fi

echo "==> Concluído. Abra o Grafana (http://localhost:3000) → Tempo para ver o trace completo."