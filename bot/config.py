from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    bot_token: str
    database_url: str
    # Telegram Payments provider token issued by BotFather for Stripe.
    # Either variable works; PAYMENTS_PROVIDER_TOKEN wins if both are set.
    payments_provider_token: str = ""
    stripe_token: str = ""

    free_video_limit: int = 3
    subscription_price_cents: int = 300  # EUR 3.00
    subscription_days: int = 30

    # Comma-separated telegram ids that always transcribe for free, no limits.
    free_user_ids: str = "540529430,1115719673"
    # Netscape-format cookies content for yt-dlp (needed for Instagram, which
    # rate-limits anonymous access). Paste the exported cookies.txt content here.
    ytdlp_cookies: str = ""

    @property
    def free_user_id_set(self) -> set[int]:
        return {int(x) for x in self.free_user_ids.replace(" ", "").split(",") if x}

    @property
    def provider_token(self) -> str:
        return self.payments_provider_token or self.stripe_token


settings = Settings()
