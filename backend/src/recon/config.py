from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration, read from RECON_* environment variables."""

    model_config = SettingsConfigDict(env_prefix="RECON_")

    database_url: str
    # Where uploaded files are kept until object storage takes over.
    storage_dir: Path = Path("data/inbox")
    max_upload_mb: int = 50
    # Serve the demo controls and sample data.
    demo: bool = False
    # A built frontend to serve at the root, if present.
    static_dir: Path | None = None
