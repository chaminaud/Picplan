import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./data/picplan.db")
    default_language: str = os.getenv("DEFAULT_LANGUAGE", "fr")
    app_env: str = os.getenv("APP_ENV", "development")
    app_secret: str = os.getenv("APP_SECRET", "local-development-secret-change-before-deploy")
    admin_password: str = os.getenv("ADMIN_PASSWORD", "")
    upload_dir: str = os.getenv("UPLOAD_DIR", "./data/uploads")
    discord_client_id: str = os.getenv("DISCORD_CLIENT_ID", "")
    discord_client_secret: str = os.getenv("DISCORD_CLIENT_SECRET", "")
    discord_bot_token: str = os.getenv("DISCORD_BOT_TOKEN", "")
    discord_guild_id: str = os.getenv("DISCORD_GUILD_ID", "")
    public_base_url: str = os.getenv("PUBLIC_BASE_URL", "")
    discord_redirect_uri: str = os.getenv(
        "DISCORD_REDIRECT_URI",
        "http://127.0.0.1:8000/auth/discord/callback",
    )


settings = Settings()
