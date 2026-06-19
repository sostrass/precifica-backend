# Blueprint dos Agentes de I.A. — Sóstrass / BlingAI

> Objetivo: orquestrar os módulos do sistema com agentes de I.A. (Gemini), sem
> transformar em "agente" aquilo que deve continuar sendo código determinístico.

## Princípio que rege tudo

Existem **duas camadas** e elas não se misturam:

- **Agentes de I.A. (Gemini):** raciocinam, geram linguagem e decidem sob
  ambiguidade. São 4. Cada um é um modelo Gemini com um *papel* (prompt de
  sistema), um *conjunto de ferramentas* que pode chamar e *contexto/memória*.
- **Ferramentas (código determinístico):** calculam preço, editam nota fiscal,
  pontuam cadastro, raspam concorrência. São exatas, auditáveis e **não são
  agentes**. O agente decide *"preciso reprecificar"*; a ferramenta faz a conta,
  sempre igual.

Regra de ouro: **um LLM nunca é a coisa que calcula dinheiro ou edita um documento
fiscal.** Ele decide e orquestra; o núcleo determinístico executa.

## Arquitetura de orquestração

```
                      ┌──────────────────────────┐
   eventos  ───────▶  │   GERENTE (orquestrador) │  ◀── travas de política
 (produto novo,       │  roteia · aprova · loga  │      (piso, cota, aprovação)
  concorrente caiu,   └─────────┬────────────────┘
  NF-e pendente,                │ delega tarefa (envelope)
  msg de cliente)               │
        ┌──────────────┬────────┴───────┬──────────────┐
        ▼              ▼                ▼              ▼
   COMERCIAL       CONTEÚDO        ATENDIMENTO     (ESTÚDIO*)
  (preço/concorr.) (copy/SEO)        (SAC)         (imagem) *opcional
        │              │                │              │
        └──────────────┴────────────────┴──────────────┘
                       │ chamam ferramentas
                       ▼
   CAMADA DE FERRAMENTAS (determinística, já construída):
   pricing · decisao · scraper(radar) · qualidade · nfe · bling · ai(gen)
```

## Protocolo de handoff (como os agentes conversam)

O Gerente delega via um **envelope de tarefa** simples (JSON):

```json
{ "tarefa": "reprecificar", "tenant_id": 12, "sku": "CX-ORG-30",
  "canal": "mercadolivre", "contexto": { ... }, "exige_aprovacao": true }
```

O especialista responde com uma **proposta**, não com uma ação executada nos casos
sensíveis:

```json
{ "acao_sugerida": "baixar", "preco_recomendado": 53.21, "motivo": "...",
  "abaixo_do_piso": false, "precisa_humano": false }
```

O Gerente então decide: **execução automática** (se for seguro e dentro das travas)
ou **fila de aprovação humana** (dinheiro/fisco). Tudo vai pro log de auditoria.

---

## 1. GERENTE (orquestrador)

- **Missão:** transformar features soltas em sistema. Recebe eventos, decide quem
  chama, junta resultados e **segura as travas de política**.
- **Gatilhos:** qualquer evento do sistema (webhook do Bling, agendador, ação do
  usuário no painel, alerta do radar).
- **Ferramentas:** os outros 3 agentes (como "ferramentas") + leitura do log de
  auditoria + leitura da config do tenant. **Não** faz trabalho de domínio direto.
- **Decide:** para quem vai a tarefa, se a ação é auto-executável ou precisa de
  aprovação, prioridade e ordem.
- **NÃO faz:** não calcula preço, não escreve copy, não responde cliente. Só
  coordena e fiscaliza.
- **Travas (as mais importantes do sistema):**
  - Nunca publica preço **abaixo do piso de viabilidade** (o motor de decisão já
    devolve `abaixo_do_piso`; o Gerente bloqueia a publicação se for o caso).
  - Alteração de **NF-e** e **publicação de preço ao vivo** = ação irreversível →
    exige aprovação humana **ou** regra rígida pré-aprovada (ex.: o toggle de modo
    automático da NF-e que já construímos).
  - Respeita a **cota diária de IA** por tenant (já existe em `ai.py`).
  - Anti-thrashing de preço: cooldown mínimo entre reprecificações do mesmo SKU.
  - Tudo logado (quem, o quê, quando, antes/depois).
- **Esboço de prompt:**
  > Você é o gerente de operações de um lojista de armarinho. Recebe eventos e
  > decide qual especialista aciona (Comercial, Conteúdo, Atendimento). Você nunca
  > calcula nem escreve nada você mesmo — você delega e revisa. Antes de aprovar
  > qualquer ação que mexa em preço publicado ou nota fiscal, confirme se está
  > dentro das travas; se não estiver, mande para aprovação humana e explique o
  > porquê em uma frase.

## 2. COMERCIAL (estrategista de preço e concorrência)

- **Missão:** interpretar o mercado e escolher a estratégia de preço por SKU/canal.
- **Gatilhos:** radar detecta movimento de concorrente; margem fora do alvo;
  produto novo sem preço de canal; rodada agendada de reprecificação.
- **Ferramentas:** `scraper.buscar_precos` (radar) · `decisao.decidir_preco` ·
  `pricing.precificar` / `precificar_reverso` / `margem_liquida` (leitura) ·
  `bling.atualizar_preco` **(travado — só via aprovação do Gerente)**.
- **Decide:** acompanhar / furar / segurar / premium; *se* vale agir agora; e
  explica o motivo em linguagem.
- **NÃO faz:** **não inventa o número.** Quem calcula é `decisao`/`pricing`. O
  agente interpreta a saída e propõe.
- **Travas:** jamais propõe abaixo do piso (o motor já trava); propõe, não publica;
  respeita cooldown.
- **Entrada → Saída:** recebe `{sku, canal, custo, preços_concorrentes}` →
  devolve `{acao, preco_recomendado, margem, motivo, precisa_humano}`.
- **Esboço de prompt:**
  > Você é o analista comercial. Use o radar e o motor de decisão para recomendar o
  > preço de cada SKU por canal. Nunca calcule à mão: chame as ferramentas. Sua
  > saída é uma recomendação com justificativa de uma linha, nunca uma publicação.

## 3. CONTEÚDO (copywriter + marketing)

- **Missão:** título/SEO, descrição anti-devolução e a blindagem jurídica. (Um
  agente só; separar marketing de copy é prematuro nesse estágio.)
- **Gatilhos:** produto novo; score de cadastro baixo; pedido manual de reescrita.
- **Ferramentas:** `ai.gerar_descricao` (com `blindar`) · `qualidade.score_cadastro`
  (pra saber o que falta) · `ai.gerar_imagem` (ou delega ao Estúdio) ·
  `bling.atualizar` da descrição **(travado — aprovação antes de publicar)**.
- **Decide:** tom, ângulo do texto, o que destacar (medidas exatas!), quando o
  cadastro está bom o suficiente.
- **NÃO faz:** não publica direto sem o ok do Gerente; não cria afirmação falsa.
- **Travas:** respeita as regras anti-devolução; a blindagem é o aviso do próprio
  lojista; respeita a cota de IA.
- **Entrada → Saída:** recebe `{produto}` → devolve `{titulo, descricao_curta,
  descricao_longa, score_antes, score_depois}`.
- **Esboço de prompt:** reaproveita o `PROMPT_BASE` que já existe em `ai.py`,
  acrescentando o checklist do `score_cadastro` como meta a bater.

## 4. ATENDIMENTO (SAC)

- **Missão:** responder o cliente no tom Sóstrass (artesanato, humano, sem robô).
- **Gatilhos:** nova mensagem/avaliação de cliente.
- **Ferramentas:** `ai.gerar_sac` · `bling.obter_produto` (contexto do item) ·
  leitura do pedido/cliente.
- **Decide:** a resposta; e **quando escalar** (caso bravo, reembolso, prazo que
  não pode prometer).
- **NÃO faz:** não promete reembolso/prazo sem escalar; não inventa política.
- **Travas:** máximo 480 caracteres, assinatura Sóstrass, sem asteriscos; casos
  sensíveis vão para humano.
- **Entrada → Saída:** recebe `{relato, produto?}` → devolve `{resposta,
  escalar: bool, motivo?}`.

## (*) ESTÚDIO — opcional, por enquanto ferramenta

Direção de arte de fotos (e vídeo depois). Hoje é quase só um wrapper de
`ai.gerar_imagem` — comece como **ferramenta que o Conteúdo aciona**. Promova a
agente próprio só quando o volume de imagem/vídeo justificar (e vídeo/Veo é caro e
assíncrono, fluxo à parte).

---

## Travas globais (valem para todos)

1. **Dinheiro e fisco passam por trava ou humano.** Publicar preço ao vivo e enviar
   alteração de NF-e ao Bling nunca são discricionários do agente.
2. **Piso de viabilidade é sagrado.** Nada abaixo dele, em nenhum canal.
3. **Cota de IA por tenant** (já implementada) — cada chamada de agente conta.
4. **Multi-tenant:** cada lojista tem seus agentes, sua cota e seus dados isolados,
   como o backend já é.
5. **Log de auditoria** de toda ação dos agentes (antes/depois, quem aprovou).

## Implementação sugerida (sem framework pesado)

No seu estágio, **não use LangGraph/CrewAI ainda** — um orquestrador fino em Python
é mais simples e muito mais fácil de depurar:

- Cada agente = uma função que monta o prompt do papel + chama o Gemini com
  *function calling*, expondo só as ferramentas daquele papel.
- O Gerente = um roteador que recebe o evento, escolhe o agente, aplica as travas e
  decide auto-executar vs. fila de aprovação.
- As ferramentas já existem: `pricing`, `decisao`, `scraper`, `qualidade`, `nfe`,
  `bling`, `ai`. Os agentes só as embrulham.

### Ordem de construção sugerida

1. **Conteúdo** e **Atendimento** primeiro — são quase 100% o que já está em
   `ai.py` (`gerar_descricao`, `gerar_sac`); viram agente com pouco esforço e dão
   valor imediato, sem risco fiscal.
2. **Comercial** — embrulha `decisao` + `radar`; só propõe, não publica.
3. **Gerente** por último — quando já houver 2–3 especialistas para coordenar; é
   ele que liga tudo e segura as travas.
