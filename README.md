# BlingAI Manager — Backend (Fase 2)

Backend único em **Python / FastAPI**, **multi-tenant** (cada usuário tem sua conta
Bling e seus dados isolados). Consolida o que estava espalhado em três versões Node:
precificação, monitoramento, scraper de concorrente e IA de descrição — tudo num só
lugar, com as chaves no servidor.

## O que tem aqui
- **Autenticação** (cadastro/login com JWT) e **token do Bling por usuário**.
- **OAuth 2.0 do Bling** com renovação automática (o `state` carrega o tenant assinado).
- **Motor de preço** (markup divisor + taxa fixa) e **margem líquida real por canal**.
- **Precificação em massa** que grava preços em lote no Bling.
- **Monitoramento** ("bolsa de valores") com status tipado (enum) por margem.
- **Concorrência**: pega o preço do concorrente (API do Mercado Livre quando o link é
  do ML; Playwright como fallback) e **simula o impacto na sua margem real**.
- **IA de descrição** (Gemini) com prompt anti-devolução e **cota por usuário/dia**.

---

## 1. App no Bling
1. Em **developer.bling.com.br/aplicativos**, crie o app e pegue Client ID/Secret.
2. **URL de redirecionamento** = `https://SEU-BACKEND.up.railway.app/auth/bling/callback`
   (idêntica à variável `BLING_REDIRECT_URI`).
3. Marque os escopos: Produtos e Notas Fiscais.

## 2. Rodar local
```bash
cp .env.example .env        # preencha Bling, JWT_SECRET e (opcional) GEMINI_API_KEY
pip install -r requirements.txt
python -m playwright install chromium     # só se for usar o scraper de sites não-ML
uvicorn app.main:app --reload --port 8000
```
Sem `DATABASE_URL`, usa SQLite (`dev.db`).

## 3. Deploy no Railway
1. Suba no GitHub e crie o projeto a partir do repo (Root Directory = `blingai-backend`).
2. Adicione **Postgres** (cria `DATABASE_URL`).
3. Variáveis: `BLING_CLIENT_ID`, `BLING_CLIENT_SECRET`, `BLING_REDIRECT_URI`,
   `JWT_SECRET`, `GEMINI_API_KEY`, `FRONTEND_ORIGIN`.
4. Para o scraper (Playwright) no Railway, garanta os browsers no build —
   ex. comando de build: `pip install -r requirements.txt && python -m playwright install --with-deps chromium`.

---

## Fluxo de uso da API
1. `POST /auth/register` ou `POST /auth/login` → recebe `token` (JWT).
2. Enviar `Authorization: Bearer <token>` nas rotas `/api/*` e `/auth/bling/*`.
3. `GET /auth/bling/login` → retorna `{url}`; o front redireciona o navegador pra essa URL.
4. Bling chama `GET /auth/bling/callback` → token salvo para aquele usuário.

| Método | Rota | Função |
|--------|------|--------|
| POST | `/auth/register` / `/auth/login` | Cria conta / autentica → JWT |
| GET | `/auth/me` | Dados do usuário logado |
| GET | `/auth/bling/login` | Retorna a URL de autorização do Bling |
| GET | `/auth/bling/callback` | Salva o token do Bling (via `state`) |
| GET | `/auth/bling/status` | Status do token do usuário |
| GET | `/api/produtos` · `/api/produtos/{id}` | Lista / detalhe de produtos |
| POST | `/api/precificar` | Preço + margem por canal (1 produto) |
| POST | `/api/precificar/lote` | Precificação em massa (grava no Bling) |
| POST | `/api/monitoramento` | Grade com margem + status tipado |
| POST | `/api/concorrencia/preco` | Preço do concorrente + simulação de margem |
| POST | `/api/ia/descricao` | Descrição via Gemini (com cota) |
| GET | `/api/nfe` | (Prévia) Notas fiscais — base da Fase 5 |

---

## Notas
- **Limites Bling**: 3 req/s, 120k/dia — já respeitados pelo rate limiter interno.
- **Concorrência**: para links do Mercado Livre, usa a API pública (mais confiável e
  evita bloqueio de bot); scraping fica só para sites sem API.
- **Margem corrigida**: `margem_liquida` desconta comissão + taxa fixa + imposto +
  cartão (a "Margem Bruta Real" do protótipo superestimava o lucro).
- **Multi-tenant hoje** = 1 usuário por conta. Para times (vários usuários por empresa),
  adicionar uma camada de Conta/Organização depois.
- **Segurança dos tokens**: hoje em texto puro no banco; em produção, restrinja o acesso
  ao Postgres e/ou criptografe o refresh token.
- **Atualizar preço**: usa `PATCH` parcial; se a conta exigir `PUT` completo, ajuste em
  `app/bling.py` (GET do produto → alterar `preco` → PUT).
