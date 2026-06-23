from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Bling
    bling_client_id: str = ""
    bling_client_secret: str = ""
    bling_redirect_uri: str = ""  # precisa bater com o cadastrado no painel do app

    # Banco (vazio -> SQLite local)
    database_url: str = ""

    # Autenticação / JWT (multi-tenant)
    jwt_secret: str = "troque-este-segredo-em-producao"
    jwt_expire_minutes: int = 60 * 24  # 1 dia

    # Gemini
    gemini_api_key: str = ""
    gemini_model: str = "gemini-1.5-pro"  # confirme o modelo atual ao subir
    ia_limite_diario: int = 50  # cota de descrições por usuário/dia

    # CORS
    frontend_origin: str = "*"

    # Radar — varredura automática em segundo plano (horas; 0 = desligado)
    radar_intervalo_horas: int = 6

    # Re-sincronização periódica do catálogo (rede de segurança além do webhook; horas, 0 = off)
    catalogo_resync_horas: int = 24

    # Shopee Open Platform (credenciais do app + loja)
    shopee_partner_id: str = ""
    shopee_partner_key: str = ""
    shopee_shop_id: str = ""
    shopee_access_token: str = ""
    shopee_refresh_token: str = ""
    shopee_base_url: str = "https://partner.shopeemobile.com"
    shopee_redirect_base: str = ""  # URL pública do backend p/ o callback OAuth (ex.: https://meu-backend.up.railway.app)

    # Apify — descoberta de concorrentes por termo em Shopee/TikTok/Shein (anti-bot).
    # Sem este token, esses canais ficam só com rastreio por URL (Radar). Veja /api/marketplaces/capacidades.
    apify_token: str = ""
    apify_actor_shopee: str = "apify/shopee-scraper"   # ajuste para o actor que você assinar
    apify_actor_tiktok: str = "apify/tiktok-shop-scraper"
    apify_actor_shein: str = "apify/shein-scraper"

    # Scraper PRÓPRIO (sem terceiros) — descoberta de concorrentes via navegador headless.
    scraper_browser: bool = True          # usa Playwright/Chromium para Shopee/TikTok/Shein
    scraper_proxy: str = ""               # proxy opcional (ex.: http://user:pass@host:porta) — datacenter costuma ser bloqueado
    scraper_timeout_ms: int = 30000


settings = Settings()
