from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    db_path: str = "data/vkdump.db"
    language: str = "en"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
