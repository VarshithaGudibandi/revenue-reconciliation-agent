from pathlib import Path
from typing import Literal

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
    # Who proposes column mappings. "auto" uses the language model when credentials are
    # present and the rules alone otherwise; "rules" never calls a model; "hybrid" requires one.
    mapper: Literal["auto", "rules", "hybrid"] = "auto"
    llm_model: str = "claude-opus-5-5"
    llm_effort: Literal["low", "medium", "high"] = "low"
    llm_fallbacks: bool = True
    # Serve model answers recorded earlier instead of calling the API.
    llm_replay: Path | None = None
    # Where reconciliation runs. "inline": inside the request that asked for it. "queue": a job
    # is queued and a separate worker (python -m recon.worker) runs it.
    jobs: Literal["inline", "queue"] = "inline"
    job_max_attempts: int = 5
    # How long a worker holds a job without a sign of life, and how often it gives one.
    job_lease_seconds: float = 60.0
    job_heartbeat_seconds: float = 15.0
    job_poll_seconds: float = 1.0
