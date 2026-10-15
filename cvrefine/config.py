from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CVREFINE_", env_file=ROOT / ".env", extra="ignore")

    # Read as CVREFINE_OPENROUTER_API_KEY only, so a machine-wide OPENROUTER_API_KEY used by
    # other projects never leaks in or overrides this project's key.
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api"

    decision_backend: Literal["jev", "chat"] = "jev"
    decision_model: str = "typesafe/jev-1.13"
    writer_model: str = "anthropic/claude-sonnet-5.5"

    data_dir: Path = ROOT / "data"

    cv_file_name: str = "main.tex"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "cvrefine.db"

    @property
    def cv_dir(self) -> Path:
        return self.data_dir / "cv"

    @property
    def cv_file(self) -> Path:
        return self.cv_dir / self.cv_file_name

    @property
    def tailored_dir(self) -> Path:
        return self.cv_dir / "tailored"


settings = Settings()
