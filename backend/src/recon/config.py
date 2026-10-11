from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration, read from RECON_* environment variables."""

    model_config = SettingsConfigDict(env_prefix="RECON_")

    database_url: str
