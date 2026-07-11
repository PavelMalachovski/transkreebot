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

    @property
    def provider_token(self) -> str:
        return self.payments_provider_token or self.stripe_token


settings = Settings()
