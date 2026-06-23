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

    # Shopee Open Platform (credenciais do app + loja)
    shopee_partner_id: str = ""
    shopee_partner_key: str = ""
    shopee_shop_id: str = ""
    shopee_access_token: str = ""
    shopee_refresh_token: str = ""
    shopee_base_url: str = "https://partner.shopeemobile.com"


settings = Settings()
