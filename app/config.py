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


settings = Settings()
