"""Environment configuration (no secrets are committed)."""
import os
from dataclasses import dataclass
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    bot_token: str
    base_url: str
    database_path: str = "data/holidays.sqlite3"
    default_timezone: str = "Europe/Moscow"
    default_birthday_time: str = "09:00"
    check_interval_seconds: int = 45

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        token = os.getenv("BOT_TOKEN", "").strip()
        # Bothost supplies DOMAIN automatically, and it may omit the scheme.
        # An explicitly configured BASE_URL always takes precedence.
        domain = os.getenv("DOMAIN", "").strip().rstrip("/")
        url = os.getenv("BASE_URL", "").strip().rstrip("/")
        if not url and domain:
            url = domain if domain.startswith("https://") else f"https://{domain}"
        if not token or "REPLACE_" in token:
            raise RuntimeError("Set BOT_TOKEN in .env")
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise RuntimeError("BASE_URL must be a public HTTPS URL")
        timezone = os.getenv("DEFAULT_TIMEZONE", "Europe/Moscow")
        ZoneInfo(timezone)
        hhmm = os.getenv("DEFAULT_BIRTHDAY_TIME", "09:00")
        validate_time(hhmm)
        return cls(
            bot_token=token,
            base_url=url,
            database_path=os.getenv("DATABASE_PATH") or os.path.join(os.getenv("DATA_DIR", "data"), "holidays.sqlite3"),
            default_timezone=timezone,
            default_birthday_time=hhmm,
            check_interval_seconds=max(15, int(os.getenv("CHECK_INTERVAL_SECONDS", "45"))),
        )


def validate_time(value: str) -> tuple[int, int]:
    try:
        hour, minute = map(int, value.split(":"))
    except (ValueError, TypeError):
        raise ValueError("Time must be HH:MM") from None
    if len(value) != 5 or value[2] != ":" or not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("Time must be HH:MM (00:00 to 23:59)")
    return hour, minute
