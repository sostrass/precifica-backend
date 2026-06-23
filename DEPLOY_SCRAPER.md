# Deploy do scraper próprio (concorrência em Shopee/TikTok/Shein)

O scraper de concorrentes usa **navegador headless (Chromium via Playwright)**. Em produção, o
Chromium precisa estar instalado. Há dois caminhos:

## Caminho 1 — nixpacks (já incluso: `nixpacks.toml`)
O `nixpacks.toml` deste projeto roda no build:
- `playwright install-deps chromium` (bibliotecas de sistema)
- `playwright install chromium` (o browser)

É só dar deploy normal no Railway. Se o build passar e o Chromium subir, está pronto.

## Caminho 2 — Dockerfile (mais robusto, recomendado se o nixpacks falhar)
Crie um arquivo `Dockerfile` na raiz do backend com o conteúdo abaixo. O Railway detecta o
Dockerfile e passa a usá-lo no lugar do nixpacks. A imagem oficial do Playwright já vem com o
Chromium e todas as libs:

```dockerfile
FROM mcr.microsoft.com/playwright/python:v1.40.0-jammy
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN python -m playwright install chromium
COPY . .
CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
```

(ajuste a tag `v1.40.0` para casar com a versão do `playwright` no requirements.txt)

## Variáveis de ambiente (Railway → Variables)
- `SCRAPER_BROWSER=true` — liga o scraper próprio (padrão já é true).
- `SCRAPER_PROXY=` — **opcional, mas importante**: IP de datacenter (Railway) costuma ser
  bloqueado pela Shopee/Shein. Se as buscas vierem vazias mesmo com o navegador OK, configure um
  **proxy residencial** aqui (ex.: `http://usuario:senha@host:porta`). Sem proxy, funciona às
  vezes; com proxy residencial, funciona de forma consistente.
- `SCRAPER_TIMEOUT_MS=30000` — timeout por página (opcional).

## Confiabilidade por canal (honesto)
- **Shopee**: usa a API interna `/api/v4/search` dentro do navegador — o caminho mais sólido.
- **Shein**: scraping por DOM — funciona, mas o layout muda; experimental.
- **TikTok Shop**: a busca web é a mais protegida/instável — experimental; o rastreio por URL no
  Radar é o caminho mais confiável aqui.

Em todos os casos, o **Radar** (rastrear a URL de um concorrente ao longo do tempo) é o plano B
que não depende de descoberta por termo.
