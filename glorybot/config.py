import logging
from decimal import Decimal
from functools import lru_cache
from zoneinfo import ZoneInfo

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    bot_token: SecretStr
    database_url: str = "sqlite+aiosqlite:///./glory.db"
    admin_ids_raw: str = Field("", alias="ADMIN_IDS")
    super_admin_ids_raw: str = Field("", alias="SUPER_ADMIN_IDS")
    business_timezone: str = "Asia/Kathmandu"
    default_currency: str = "NPR"
    default_price_per_squad: Decimal = Decimal("375")
    max_squads_per_order: int = 10
    guild_uid_min_len: int = 5
    guild_uid_max_len: int = 15

    provider_mode: str = "mock"  # mock | live
    glory_api_enabled: bool = False
    glory_api_base_url: str = "https://ffglory.pro"
    glory_api_key: SecretStr = SecretStr("")
    glory_api_master_key: SecretStr = SecretStr("")
    glory_api_timeout: float = 20.0

    esewa_number: str = "9827679425"
    esewa_name: str = "Anil"
    khalti_number: str = "9827679425"
    khalti_name: str = "Anil"
    payment_mode: str = "mock"  # mock | live  (must match provider_mode)
    apinepal_base_url: str = "https://apinepal.com"
    apinepal_secret_key: SecretStr = SecretStr("")
    apinepal_public_key: str = ""
    apinepal_qr_provider: str = "fonebiz"  # fonebiz | fonepay
    payment_ui_minutes: int = 15
    payment_watch_minutes: int = 30  # APINepal keeps the QR valid 30 min; late payments are still honored
    button_colors: bool = True            # colored buttons (Telegram clients after Feb 2026)
    premium_emoji_buttons: bool = False   # needs bot-owner Premium or Fragment usernames
    premium_emoji_ids: str = ""           # "fire=5368...,trophy=5420..."
    service_window: bool = True           # closed from Sunday 9 PM until Monday 12 AM (business timezone)
    website_url: str = "www.fflevel.xyz"
    web_enabled: bool = True              # BNY SHOP website, runs inside the bot process
    web_host: str = "0.0.0.0"
    web_port_raw: int = Field(0, alias="WEB_PORT")   # 0 = use the panel's SERVER_PORT, else 8080
    public_url: str = ""                  # e.g. https://shop.example.com (only used in messages)
    support_username: str = "ceobny"
    payment_instructions: str = "Contact support for payment instructions."
    log_level: str = "INFO"

    @property
    def web_port(self) -> int:
        import os
        return self.web_port_raw or int(os.environ.get("SERVER_PORT") or 8080)

    @property
    def admin_ids(self) -> set[int]:
        return {int(x) for x in (self.admin_ids_raw + "," + self.super_admin_ids_raw).split(",") if x.strip()}

    @property
    def super_admin_ids(self) -> set[int]:
        return {int(x) for x in self.super_admin_ids_raw.split(",") if x.strip()}

    @property
    def emoji_map(self) -> dict[str, str]:
        out = {}
        for part in self.premium_emoji_ids.split(","):
            if "=" in part:
                k, v = part.split("=", 1)
                if k.strip() and v.strip().isdigit():
                    out[k.strip()] = v.strip()
        return out

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.business_timezone)

    @model_validator(mode="after")
    def _validate(self):
        if not self.admin_ids:
            raise ValueError("ADMIN_IDS or SUPER_ADMIN_IDS must contain at least one Telegram numeric ID")
        if self.provider_mode not in ("mock", "live"):
            raise ValueError("PROVIDER_MODE must be 'mock' or 'live'")
        if self.provider_mode == "live":
            if not (self.glory_api_enabled and self.glory_api_key.get_secret_value()
                    and self.glory_api_master_key.get_secret_value()):
                raise ValueError("Live mode requires GLORY_API_ENABLED=true, GLORY_API_KEY and GLORY_API_MASTER_KEY")
        if self.payment_mode not in ("mock", "live"):
            raise ValueError("PAYMENT_MODE must be 'mock' or 'live'")
        if (self.payment_mode == "live") != (self.provider_mode == "live"):
            raise ValueError("PAYMENT_MODE and PROVIDER_MODE must both be 'live' or both 'mock' "
                             "(real money must never buy fake orders; fake payments must never spend real credits)")
        if self.payment_mode == "live" and not self.apinepal_secret_key.get_secret_value():
            raise ValueError("Live payments need APINEPAL_SECRET_KEY")
        if not 1 <= self.max_squads_per_order <= 10:
            raise ValueError("MAX_SQUADS_PER_ORDER must be 1-10")
        if self.default_price_per_squad <= 0:
            raise ValueError("DEFAULT_PRICE_PER_SQUAD must be positive")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()


def setup_logging(level: str) -> None:
    import os
    from logging.handlers import RotatingFileHandler
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    try:
        os.makedirs("logs", exist_ok=True)
        handlers.append(RotatingFileHandler("logs/bot.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"))
    except OSError:
        pass
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=handlers)
    # httpx logs full URLs at INFO, and this provider takes api_key as a query param.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
