# Manual de Deploy — GitHub + Railway + Postgres

Guia do zero. O **backend** (Python/FastAPI) é o que vai para o Railway com o
Postgres. O **frontend** (React) fica em outro repositório (você já criou o
`precifica-frontend`) e pode ir na Vercel ou também no Railway.

> Você só tinha o repositório do frontend. Aqui vamos criar **um repositório novo
> para o backend** — é o caminho mais simples para o Railway.

---

## Pré-requisitos (uma vez só)

1. Conta no GitHub (você já tem).
2. Conta no Railway — entre em https://railway.app e faça login **com o GitHub**.
3. Git instalado no seu computador (teste no terminal: `git --version`).
   - Sem git / não quer terminal? Use o **GitHub Desktop**
     (https://desktop.github.com) — explico a alternativa no Passo 1B.
4. Em mãos: suas credenciais do **Bling** (Client ID e Secret, do app em
   developer.bling.com.br) e sua **chave do Gemini**.

---

## PARTE 1 — Subir o backend no GitHub

### Passo 1A — Pelo terminal (recomendado)

1. No GitHub, clique em **New repository**. Nome: `precifica-backend`. Deixe
   **vazio** (sem README, sem .gitignore — o nosso já tem). Clique em
   **Create repository**.

2. No seu computador, abra o terminal **dentro da pasta do backend** (a pasta que
   contém `app/`, `Procfile`, `requirements.txt`) e rode:

   ```bash
   git init
   git add .
   git commit -m "Backend inicial: precificação, decisão, NF-e, IA"
   git branch -M main
   git remote add origin https://github.com/SEU_USUARIO/precifica-backend.git
   git push -u origin main
   ```

   Troque `SEU_USUARIO` pelo seu usuário do GitHub. Se pedir login, use seu usuário
   e um **Personal Access Token** como senha (GitHub > Settings > Developer
   settings > Personal access tokens).

3. Recarregue a página do repositório no GitHub — seus arquivos devem aparecer.
   **Confira que o `.env` NÃO subiu** (o `.gitignore` impede isso de propósito).

### Passo 1B — Sem terminal (GitHub Desktop)

1. Crie o repositório `precifica-backend` no GitHub (como no 1A, passo 1).
2. Abra o GitHub Desktop > **File > Clone repository** > escolha `precifica-backend`.
3. Copie todos os arquivos do backend para dentro da pasta clonada.
4. No GitHub Desktop, escreva uma mensagem de commit, clique em **Commit to main**
   e depois em **Push origin**.

---

## PARTE 2 — Criar o projeto no Railway

1. No Railway, clique em **New Project** > **Deploy from GitHub repo**.
2. Autorize o Railway a acessar seus repositórios e escolha `precifica-backend`.
3. O Railway detecta Python automaticamente e usa o **Procfile** para iniciar:
   `uvicorn app.main:app --host 0.0.0.0 --port $PORT`. Não precisa configurar
   comando de start.
4. O primeiro build vai rodar. **Pode falhar a primeira vez** por falta das
   variáveis de ambiente — normal, vamos adicioná-las nos próximos passos.

> Se o repositório fosse um monorepo (backend dentro de uma subpasta), você
> definiria **Settings > Root Directory** apontando para a pasta. Como criamos um
> repo só do backend, não precisa.

---

## PARTE 3 — Adicionar o Postgres

1. Dentro do projeto no Railway, clique em **New** (ou **+ Create**) >
   **Database** > **Add PostgreSQL**.
2. O Railway cria o banco e expõe uma variável chamada `DATABASE_URL` no serviço do
   Postgres.
3. Agora ligue o backend ao banco: abra o serviço **do backend** > aba
   **Variables** > **New Variable** e crie:

   - Nome: `DATABASE_URL`
   - Valor (referência ao Postgres): `${{Postgres.DATABASE_URL}}`

   Esse `${{Postgres.DATABASE_URL}}` é a sintaxe de referência do Railway: ele
   injeta a URL real do banco automaticamente.

> O backend já cria as tabelas sozinho na primeira subida (não precisa de migração)
> e já normaliza `postgres://` para `postgresql://`. Se a `DATABASE_URL` ficar
> vazia, ele cairia para SQLite local — por isso a referência acima é importante.

---

## PARTE 4 — Variáveis de ambiente do backend

Ainda na aba **Variables** do serviço do backend, adicione (além da `DATABASE_URL`
do passo anterior):

| Variável | O que colocar |
|---|---|
| `BLING_CLIENT_ID` | Client ID do seu app no Bling |
| `BLING_CLIENT_SECRET` | Client Secret do seu app no Bling |
| `BLING_REDIRECT_URI` | (preenchemos na Parte 5, após gerar o domínio) |
| `JWT_SECRET` | uma frase longa e aleatória (segredo de login) |
| `GEMINI_API_KEY` | sua chave do Gemini |
| `GEMINI_MODEL` | ex.: `gemini-1.5-pro` (confirme o modelo atual) |
| `IA_LIMITE_DIARIO` | ex.: `50` (cota diária de IA por usuário) |
| `JWT_EXPIRE_MINUTES` | ex.: `1440` (1 dia) |
| `FRONTEND_ORIGIN` | (preenchemos na Parte 6, a URL do frontend) |

Ao salvar, o Railway **re-builda** automaticamente.

---

## PARTE 5 — Gerar o domínio e configurar o Bling

1. No serviço do backend: **Settings > Networking > Generate Domain**. O Railway
   te dá uma URL pública, algo como `https://precifica-backend-production.up.railway.app`.

2. Volte em **Variables** e preencha:
   - `BLING_REDIRECT_URI` = `https://SUA-URL.up.railway.app/auth/bling/callback`

3. **No painel do Bling** (developer.bling.com.br, no seu app): cadastre essa
   **mesma** URL de redirecionamento. Ela precisa bater exatamente, senão o OAuth
   falha. Selecione os escopos que o sistema usa (Produtos e Notas Fiscais).

---

## PARTE 6 — Testar

1. Abra no navegador: `https://SUA-URL.up.railway.app/health` — deve responder OK.
2. Crie um usuário (use um cliente como o painel, ou um `curl`):

   ```bash
   curl -X POST https://SUA-URL.up.railway.app/auth/register \
     -H "Content-Type: application/json" \
     -d '{"email":"voce@exemplo.com","senha":"umasenhaboa"}'
   ```

   A resposta traz um **token** — é o JWT que o front usa no header
   `Authorization: Bearer <token>`.
3. Autorize o Bling: abra `https://SUA-URL.up.railway.app/auth/bling/login`
   (autenticado), siga o fluxo, e confira em `/auth/bling/status`.

---

## PARTE 7 — Frontend (resumido)

O `precifica-frontend` (React/Vite) aponta para o backend:

1. No frontend, defina a variável `VITE_API_URL` = a URL do backend no Railway.
2. Deploy mais fácil: **Vercel** — importe o repositório `precifica-frontend`,
   defina `VITE_API_URL` nas Environment Variables e faça o deploy. (Dá para usar o
   Railway também, como serviço estático.)
3. Pegue a URL final do frontend e coloque em `FRONTEND_ORIGIN` (Parte 4) do
   backend, para o CORS liberar o navegador. Pode ser a URL exata ou `*` para
   liberar tudo (menos seguro).

---

## Armadilhas comuns (troubleshooting)

- **Playwright:** está no `requirements.txt`, mas o Railway não instala os
  navegadores. O deploy **não quebra** (o import é tardio). Só o *fallback* do
  radar com Playwright não funciona até você instalar os navegadores; o caminho
  normal (requests + BeautifulSoup) funciona. Para um primeiro deploy enxuto, você
  pode até remover a linha `playwright` do `requirements.txt`.
- **`code` do Bling expira em ~1 min:** o fluxo OAuth precisa trocar o code logo
  após o redirect. Se demorar, refaça o login do Bling.
- **Build falhou na 1ª vez:** quase sempre é variável de ambiente faltando. Veja os
  **Deploy Logs** no Railway, ajuste as Variables e re-deploy.
- **Plano gratuito dorme:** o serviço pode hibernar sem uso; a primeira chamada
  depois disso demora alguns segundos para "acordar".
- **Nunca suba o `.env`:** o `.gitignore` já impede. Os segredos vivem só nas
  Variables do Railway.
