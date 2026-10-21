# 🗺️ Roadmap — Próximos Serviços do Lab (E-commerce)

> Evolução do laboratório depois do core atual (catalog, cart, order, payment, invoice/logistics/warranty workers).
> Objetivo: completar a "história" de um e-commerce real e adicionar os problemas de arquitetura que fazem sentido didaticamente.
> Nada aqui foi implementado — é um plano para validar/priorizar antes de codar.

## Visão Geral

| # | Serviço | Tipo | Problema que ataca | Prioridade |
|---|---|---|---|---|
| 1 | `auth` (Customer + JWT) | API | Identidade/autenticação de ponta a ponta | Alta |
| 2 | `inventory` (split do catálogo) | API | Responsabilidade única: vitrine vs. estoque | Alta |
| 3 | `notification` | Worker | Canal de e-mail/SMS após pagamento | Média |
| 4 | `order-history` (read API) | API (read model) | Consulta de pedidos por usuário (CQRS simples) | Média |
| 5 | Busca/filtro no `catalog` | Evolução | Leitura do catálogo com query params | Baixa |

Ordem sugerida: **1 → 2 → 3 → 4 → 5** (auth primeiro porque os demais ganham contexto de usuário; inventory em seguida porque mexe no core; depois os assíncronos/leitura).

---

## 1. `auth` — Customer + JWT

### Objetivo
Acabar com o carrinho "string livre" e dar dono aos dados: usuário registra, loga, e o token JWT segue em todos os serviços.

### Escopo
- `POST /api/v1/auth/register` — cria usuário (senha com hash bcrypt/argon2)
- `POST /api/v1/auth/login` — devolve JWT de acesso (curta expiração)
- `GET /api/v1/auth/me` — perfil do token (validação)
- Banco próprio `auth_db` (users com id, email, senha_hash)
- Verificação do JWT: nginx não valida JWT nativo → validar nos serviços via `lab_common` (decorator `@require_auth` que lê `Authorization: Bearer` e coloca `userId` no contexto/g)
- `cart` passa a pertencer a um `userId` (o `cartId` livre morre ou vira derivado do usuário)
- `order` registra `user_id` no pedido

### Integrações
- Gateway: rota `/api/v1/auth/*` nova (nginx)
- `cart`, `order` passam a exigir token
- JWT como demonstration de **propagação de contexto de segurança** entre serviços (não re-valida no gateway, cada serviço valida)

### Conceitos didáticos
- Autenticação vs autorização
- JWT: claims, expiração, secret/par de chaves
- Hashing de senha; por que nunca logar/serializar a senha
- Tokens em microsserviços: validar localmente (sem chamada a cada request) vs. introspectar

### Estimativa
~1 dia de trabalho. Mexe no cart/order existentes (mudança pequena, mas quebra o contrato atual → cuidar com o Swagger combinado e scripts demo).

---

## 2. `inventory` — Split Catálogo x Estoque

### Objetivo
Tirar `stock`/`reserve`/`release` do `catalog`: o catálogo vira só vitrine (name/price/atributos) e o **inventory é o dono do estoque** e de todas as mutações de quantidade.

### Escopo
- Novo serviço `inventory` + `inventory_db` (SKU = `product_id`, `quantity`, `reserved`)
- API (interna, chamada pelo `order`; exposta no gateway como `/api/v1/inventario/*` se quiser ver):
  - `POST /products/:id/reserve` (baixa reserva com timeout)
  - `POST /products/:id/release` (compensação)
  - `POST /products/:id/confirm` (baixa definitiva pós-pagamento — hoje não existe!)
  - `GET /products/:id/stock` e `GET /stock` (listagem/consulta)
- `catalog` mantém `GET /products*`, mas **sem campo `stock`** no modelo principal (ou com `stock` vindo de consulta opcional ao inventory)
- Seed migra: produtos no catálogo, quantidades no inventory

### Integrações
- Criação de produto: `catalog` publica evento `product.created` (ou chama callback) para o inventory criar o SKU — primeiro **problema real de consistência entre serviços** do lab
- `order` deixa de chamar `catalog/.../reserve` e passa a chamar `inventory` (mantendo saga + compensação `release`)
- `cart` continua só lendo o catálogo (preço) — e pode passar a mostrar "em estoque?" via inventory

### Conceitos didáticos
- **Responsabilidade única por dados**: catálogo é lido freneticamente, estoque é escrito freneticamente — escalam diferente
- Consistência eventual via eventos (catálogo criado → estoque nasce depois)
- **Confirm/release de reserva** (two-phase simplificado) dentro da saga do pedido
- Decisão de modelagem: inventory separado vs. campo no catálogo (trade-offs na mesa)

### Estimativa
~1–2 dias. É a mudança mais invasiva (modela o `catalog`, `order`, seed e scripts demo). Fazer logo depois do auth para não arrumar duas vezes.

---

## 3. `notification` — Worker de E-mail/SMS

### Objetivo
Fechar a experiência: usuário recebe notificação de pedido pago/rejeitado (simulada por log/arquivo), demonstrando mais um consumidor independente do topic.

### Escopo
- Worker `notification-worker` (mesmo padrão de `invoice-worker` via `lab_common.worker`)
- Consome `order.paid` e `order.rejected` da exchange `orders.events`
- "Envia" e-mail/SMS: log estruturado + tabela `notifications` no `notification_db` (status da entrega simulada)
- Endpoint opcional `GET /notifications/:userId` para o professor mostrar o que foi "enviado"

### Integrações
- Nova fila no RabbitMQ (bind nas duas routing keys)
- Precisa do `userId` no payload dos eventos (caso com o item 1 — auth)
- Métricas `notification_sent_total` + dashboard

### Conceitos didáticos
- Fan-out de eventos para múltiplos consumidores independentes (invoice, logistics, warranty, notification)
- Simulação de canal externo (latência/falha configuráveis, DLQ)
- Por que notificação é fire-and-forget e NÃO participa da saga

### Estimativa
~meio dia. Praticamente cola do invoice-worker.

---

## 4. `order-history` — Read API de Pedidos

### Objetivo
Consultas que o `order` não atende: "meus pedidos", com junção de status de entrega, sem sobrecarregar o banco transacional de escrita.

### Escopo
- Novo serviço (ou simples extensão do `order`) que materializa pedidos por usuário
- Opção A (rápida): `GET /api/v1/pedidos?userId=...` com índice no `order_db`
- Opção B (didática): `order-history` consome `order.paid/rejected` e monta um **read model** próprio (`order_history_db`), com dados enriquecidos (itens, NF, status de entrega)

### Integrações
- Opção B: consome os mesmos eventos dos workers + eventos de status da entrega (`delivery.scheduled` etc.)
- Com auth (item 1), o filtro é por `userId` do token

### Conceitos didáticos
- **CQRS simplificado**: separar modelo de escrita (order) do de leitura (order-history)
- Read model como cache/especialização de consulta
- Como o mesmo evento alimenta N consumidores com propósitos diferentes

### Estimativa
~1 dia na opção B. Rende muito em aula se o professor quiser mostrar CQRS sem virar Kafka.

---

## 5. Busca/Filtro no `catalog`

### Objetivo
A leitura do catálogo deixar de ser "listar tudo": filtros e busca simples.

### Escopo
- `GET /api/v1/catalogo/products?name=&category=&minPrice=&maxPrice=&sort=`
- Campo `category` no produto (migração simples do seed)
- Paginação (`page`/`limit`) para demonstrar volume

### Integrações
- Nada assíncrono — só o contrato do catálogo muda (Swagger combinado acompanha)

### Conceitos didáticos
- Query params/validação no contrato OpenAPI
- Índices e quando "busca de verdade" (Elasticsearch) passa a ser necessária

### Estimativa
~2–3 horas. Menor prioridade, dá para fazer em qualquer momento.

---

## Impactos Transversais (sempre que mexer)

- [ ] Atualizar `docs/openapi/ecommerce.yaml` (ReDoc + Swagger UI combinados)
- [ ] Atualizar tabela de serviços e diagrama de arquitetura do README
- [ ] Atualizar `scripts/demo_flow.sh` / `load_orders.sh` (auth muda o fluxo!)
- [ ] Dashboard Grafana: metrics novos (`auth_*`, `inventory_*`, `notification_sent_total`)
- [ ] Prometheus: novos jobs (`auth`, `inventory`, `order-history`, workers)
- [ ] nginx.conf: novas rotas `/api/v1/*`
- [ ] `config/postgres/init.sql`: novos databases