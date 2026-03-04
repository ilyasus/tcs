import os


class Settings:
    def __init__(self) -> None:
        self.db_path = os.getenv("APP_DB_PATH", "./tesla_wall_charger.db")
        self.poll_interval_seconds = int(os.getenv("POLL_INTERVAL_SECONDS", "15"))
        self.tesla_base_url = os.getenv("TWC_BASE_URL", "http://192.168.1.167")
        self.request_timeout_seconds = float(os.getenv("TWC_TIMEOUT_SECONDS", "4"))
        self.app_timezone = os.getenv("APP_TIMEZONE", "America/Los_Angeles")
        self.default_rate_plan = os.getenv("APP_RATE_PLAN", "EV2-A")


settings = Settings()
