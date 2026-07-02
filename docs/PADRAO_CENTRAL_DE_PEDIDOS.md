# Padrão "Central de Pedidos" — Enterprise Cockpit (Precifica AI)

> **Objetivo.** Este documento define o padrão visual, de UX e de arquitetura da
> *Central de Pedidos* do Precifica AI. Ele foi construído primeiro para o
> **Mercado Livre** e é o **modelo de referência** para replicar em **Shopee,
> Shein, TikTok Shop, Nuvemshop, Amazon, Temu** e demais canais integrados ao
> **Hub do Bling** (fonte de produto/preço/NF-e) e ao **Hub Precifica AI**
> (precificação por faixa e cockpit de operação).

> **Diretriz permanente.** Todo painel do projeto sai no **máximo padrão
> Enterprise**: layout rico em detalhes, UX/UI de alto nível, sempre
> surpreendente. Ao redesenhar, **nunca remover** recursos existentes (busca,
> gráficos, resumo, botões de personalização etc.) — todos permanecem e são
> elevados juntos.

---

## 1. Arquitetura reutilizável — o que é compartilhado × específico do canal

O painel foi desenhado para separar claramente **o que muda por marketplace** do
**que é comum a todos**. Ao integrar um novo canal, só a camada de *adapter*
precisa ser escrita; UI, precificação e hub do Bling são reaproveitados.

| Camada | Reuso | Onde vive |
|---|---|---|
| **UI do cockpit** (abas, filtros, KPIs, gráfico, resumo, cards, drawer) | **100% compartilhado** | Componentes React (padrão deste doc) |
| **Motor de precificação** (faixas por canal, imposto, cartão, embalagem) | **100% compartilhado** | `precificacao.py` → `margem_real_canal(cfg, canal, preco, custo)` |
| **Hub do Bling** (produto/SKU, Preço Bling, custo, NF-e) | **100% compartilhado** | `catalogo.py`, `nfe.py` |
| **Adapter do canal** (buscar pedidos, envios, tarifas, mensagens do marketplace) | **específico** | `mercadolivre.py`, `shopee.py`, … (um por canal) |
| **Modelo comum de pedido** (o "contrato" que a UI consome) | **compartilhado** | ver §10 |

**Regra de ouro:** o adapter do canal traduz a API do marketplace para o
**modelo comum de pedido** (§10). A partir daí, tudo é igual em qualquer canal.

---

## 2. Design tokens

Tema escuro, uma única paleta. Variáveis CSS (nunca usar `color-mix`; Safari
antigo/High Sierra):

```
--bg:#120910  --surface:#1c1018  --surface2:#24141d
--fg:#f6eef3  --dim:#c9b6c2  --faint:#8c7a86
--accent:#d6007f (rosa Precifica)   --ok:#2FD98D   --warn:#E0A23C   --danger:#ff7a7a
--ml:#F2C200 (amarelo do canal; troca por canal: Shopee #EE4D2D, Amazon #FF9900, etc.)
--glass-bg:rgba(255,255,255,.05)  --glass-border:rgba(255,255,255,.09)  --glass-hover:rgba(255,255,255,.08)
```

- **Cor de canal** (`--ml`) é o único token que muda por marketplace (badge do
  canal, botão primário de etiquetas, destaque "Full/expede pelo canal").
- **Tipografia:** base 13px; título serifado (Georgia) 17px; valores de KPI
  18px; números sempre `font-variant-numeric: tabular-nums` (classe `.num`).
- **Ícones:** Lucide apenas. **Sem emoji.** Comunicação sempre em pt-BR.
- **Raios:** cards 18px, blocos internos 12–14px, chips/pílulas 999px.
- **Vidro:** `.glass` usa `backdrop-filter` → **modais/menus fixos via
  `createPortal`** (o backdrop-filter aprisiona `position:fixed`).

---

## 3. Estrutura do painel (de cima para baixo)

1. **Header** — logo + título "Central de pedidos" + badge do canal; subtítulo;
   à direita os botões de ação: **Personalizar**, Separação, Imprimir,
   **Etiquetas em lote (N)** (primário, cor do canal), Envios/Full, NF-e.
2. **Abas segmentadas** (etapas do envio) — trilho com ícone + rótulo +
   contador em pílula; ativa em degradê rosa com brilho. Abas:
   Todos · A despachar hoje · Próximos dias · Aguardando NF-e · Em trânsito ·
   Finalizados · Cancelados.
3. **Toolbar de filtros** — pagamento (mini-segmentado) + toggles com **estado
   on/off nítido**: "Sem dados fiscais" (vermelho), "Devoluções", "Não lidas"
   (rosa). À direita: período **7d/15d/30d + calendário customizável**
   (Dia/Mês/Ano) e a preferência **Confortável/Compacto**.
4. **Faixa operacional — Coleta de hoje** — janela (ex.: 15h–17h), horário de
   corte, transportadora e **código de autorização do dia**.
5. **KPIs** (6) — Pedidos · Receita · Ticket médio · Taxas mkt · Frete ML ·
   **Líquido** (destaque verde). Cada um com ícone em quadro + **mini-tendência
   ▲/▼ vs período anterior** (verde sobe bom / vermelho sobe custo).
6. **Analytics** — **Receita por dia** (barras em degradê, valor no topo,
   **dia de pico destacado**) + **Resumo** da aba (Pedidos, Unidades, Full e
   **Margem média em anel/donut**).
7. **Busca** — campo + escopo (Tudo/#Pedido/Comprador/Produto·SKU) +
   **Ordenar** (Recentes/Antigos/Prioridade de despacho) + atualizar.
8. **Lista de cards** paginada.

---

## 4. O card de pedido (anatomia)

Dois modos, **escolha do operador** (preferência persistida):

**Confortável** (análise):
- **Linha 1:** checkbox · miniatura (52px) · título + selos (un., Full, novo,
  estornado) · **valor vendido** (à direita) + chevron.
- **Identificação:** #pedido · data · comprador · SKU (dim).
- **Faixa de status** (com **barra colorida na lateral esquerda** do card por
  estado): pílula de estado do envio + **contagem regressiva** + status de
  pagamento + NF-e. Estados: *Pronto p/ despachar* (verde) · *Em preparação* ·
  *Coleta DD/MM* (próximos dias) · *Etiqueta disponível DD/MM* (buffered) ·
  *Falta nota fiscal* (vermelho) · *Em trânsito · rastreio* · *Entregue · data*.
- **Faixa de KPIs (4):** Vendido (com *alvo Bling*) · Taxas mkt · Frete ML ·
  **Sobra/Líquido** (com *vs alvo* e/ou **donut de margem** on/off).

**Compacto** (despacho em massa): mesmo conteúdo em **uma linha** — miniatura +
produto + chips de status + métricas curtas (Taxas, Sobra·margem) + valor.
Cabem muitos por tela.

**Contagem regressiva:** para *A despachar hoje*, conta até o **horário de corte
da coleta** ("faltam 2h40 p/ coleta"); para *Próximos dias*, "faltam N dias".
Âmbar perto do corte, vermelho na reta final.

---

## 5. O drawer enriquecido (detalhe do pedido)

- **Moldura:** borda lateral **fininha em degradê** (rosa → neutro).
- **Blocos:** cada seção é um **cartão com fundo em degradê sutil** (informação
  "encaixada", não dispersa), título com ícone em rosa.
- **Cabeçalho:** comprador + #pedido + status; botões (Baixar etiqueta [primário],
  Imprimir, NF-e Bling, Abrir no canal).
- **Chips de fato rápido:** Full · Pago · Entrega no prazo · Coleta 15h–17h ·
  (não) afeta reputação.
- **Seções:**
  1. **Linha do tempo do envio** + selo de **SLA** (no prazo/atrasado).
  2. **Entrega** — previsão de entrega, prazo limite do comprador,
     transportadora, **link de rastreio**, endereço real.
  3. **Pagamento** — método, parcelas, aprovado em.
  4. **Produtos** — imagem, variação, SKU, qtd, preço.
  5. **Repasse e margem** — receita − taxas − frete = sobra; alvo (Preço Bling);
     **donut de margem**; link "ver faturamento".
  6. **Nota fiscal** — situação (emissão no Bling) + dados fiscais do comprador.
  7. **Mensagens** — thread + resposta (350 caracteres).
  8. **Sinais** — alertas de fraude, mediação, catálogo (via tags do pedido).

---

## 6. Modelo financeiro — precificação (CANÔNICO, igual em todos os canais)

**Modelo base-venda.** O **Preço Bling** é o **líquido-alvo** (o quanto deve
"sobrar" para a empresa). O preço no marketplace é o *gross-up* que cobre as
taxas. O **custo do produto (COGS)** é o `precoCusto` do Bling quando houver.

Fórmula única (`precificacao.margem_real_canal`), idêntica à usada na aba Shopee:

```
faixa   = faixa cujo teto ("ate") cobre o preço vendido, no canal
pct     = (comissao% + fixo_pct% da faixa) + imposto% + cartao%        [da Config]
fixos   = fixo R$ da faixa + embalagem R$                              [da Config]
liquido = preco * (1 - pct/100) - fixos
taxas   = preco - liquido
lucro   = liquido - custo_produto        (só quando há custo cadastrado)
margem% = lucro / preco * 100            (em branco quando sem custo)
```

- As **faixas por canal** ("ATÉ R$ X → comissão % + fixo R$ + fixo %") vêm da
  **aba Configurações** (`PrecificacaoConfig.canais`). Cada marketplace tem o
  seu padrão de custo/faixa — **respeitar sempre o que está na config do canal**.
- Nunca tratar o **Preço Bling** como custo. Ele é **alvo/referência**.
- Pedido **cancelado** = tarifa estornada pelo ML → marcado "estornado" e **fora
  dos totais**.

**Validação de referência (exemplo do cliente):** faixa 20% + R$6,50; vendido
R$20,62 → taxas R$10,62 → **sobra R$10,00** (o Preço Bling). Coberto por teste.

---

## 7. Modelo de envio / etapas (baldes)

- `/orders/search` **não** traz status de envio → **cache de envio**
  (`ml_envio_cache`) alimentado por **webhooks** (tópico `shipments`) +
  **backfill** sob demanda (lotes pequenos, commit por envio + rollback no erro
  — Postgres aborta a transação inteira se um item falha).
- **Prazo de despacho/coleta** vem do sub-recurso `/shipments/{id}/lead_time`
  (`estimated_handling_limit`) — buscado só para envios acionáveis.
- **Classificação** (a despachar hoje / próximos / aguardando NF-e / trânsito /
  finalizado / cancelado) é feita a partir do **estado real do envio**.
- **Coleta** (janela + corte + código) via `/users/{id}/shipping/schedule/{lt}`.

---

## 8. Como adicionar um novo marketplace (checklist do adapter)

Para **Shopee / Shein / TikTok / Nuvemshop / Amazon / Temu / …**:

1. **Config do canal** — garantir o canal em `PrecificacaoConfig.canais` com as
   **faixas corretas** (comissão/fixo por faixa de preço) daquele marketplace.
   Definir a **cor do canal** (`--ml` equivalente) e o badge.
2. **Adapter** (`<canal>.py`) — implementar, contra a API do marketplace:
   - buscar **pedidos** do período (com paginação);
   - buscar/observar **envios** (status, prazo, rastreio, endereço) via webhook
     + backfill, guardando em cache equivalente;
   - **tarifa real** por pedido (quando a API expõe);
   - **mensagens**, **devoluções/mediações**, **agenda de coleta** (quando houver).
3. **Mapear para o modelo comum de pedido** (§10) — é isso que a UI consome.
4. **Reusar**: `margem_real_canal(cfg, "<canal>", preco, custo)` para margem;
   `catalogo` (Preço Bling/custo/SKU) e `nfe` (status/emissão no Bling).
5. **UI**: montar a Central de Pedidos do canal **com os mesmos componentes**
   deste padrão. Só a cor do canal e os rótulos específicos mudam.
6. **Qualidade** (obrigatório): AST + import + teste funcional no backend;
   `npm run build` no frontend; **política de não-regressão** (não editar o
   painel de outro canal como efeito colateral); mostrar **mockup** antes de
   mudança visual não trivial.

---

## 9. Checklist de qualidade (todo canal)

- [ ] Backend: `python -c "import ast; ast.parse(...)"` + import + teste funcional.
- [ ] Frontend: `npm run build` limpo, **um** `export default`.
- [ ] Precificação bate com a config do canal (teste com um pedido conhecido).
- [ ] Baldes batem com o painel do marketplace.
- [ ] Nada removido do painel ao redesenhar (busca, gráfico, resumo, botões).
- [ ] pt-BR, Lucide, sem emoji, sem `color-mix`, modais via `createPortal`.

---

## 10. Modelo comum de pedido (contrato UI ⇄ adapter)

O adapter de cada canal devolve pedidos neste formato; a UI não conhece a API do
marketplace, só este contrato:

```jsonc
{
  "id": "<id do pedido no canal>",
  "pack_id": "<agrupador, se houver>",
  "date_created": "ISO",
  "pago_em": "ISO|null",
  "status": "paid|payment_required|confirmed|cancelled|...",
  "buyer": { "nickname": "...", "id": "..." },
  "is_full": false,                 // canal expede (Full/FBA/etc.)
  "itens": [{ "sku","titulo","imagem","quantidade","unit_price",
              "preco_bling","ml_preco","liquido","margem" }],
  "envio": {                        // do cache (webhook + backfill), pode ser null
     "status","substatus","logistic_type","handling_limit","buffering_date",
     "date_ready","date_shipped","date_delivered","tracking_number",
     "custo_vendedor","receiver_*","fiscal_pendente","devolucao" },
  "balde": "hoje|proximos|fiscal|transito|finalizado|cancelado|sincronizando",
  "resumo": {                       // margem no modelo base-venda (§6)
     "receita","tarifa","taxas","preco_bling","frete_vendedor",
     "liquido","margem","unidades","estornado" }
}
```

E stats do período: `{ pedidos, receita, ticket_medio, unidades, tarifas,
frete_vendedor, custos_ml, impostos, liquido, margem, baldes{...},
fiscal_pendentes, devolucoes, sincronizando }`.

---

*Este padrão é a base. Cada novo marketplace herda tudo: só escreve o adapter e
ajusta cor/faixas do canal.*
