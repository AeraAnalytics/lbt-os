
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Supabase
    supabase_url: str
    supabase_service_key: str

    # Clerk
    clerk_secret_key: str
    clerk_publishable_key: str
    clerk_webhook_secret: str

    # LLM — supports OpenAI or Ollama
    # For Ollama: set llm_provider=ollama, ollama_model=llama3.2, leave openai_api_key empty
    # For OpenAI: set llm_provider=openai, openai_api_key=sk-...
    llm_provider: str = "ollama"          # "openai" | "ollama"
    openai_api_key: str = "not-needed"   # only required when llm_provider=openai
    openai_model: str = "gpt-4o-mini"    # used when llm_provider=openai, plan=pro
    openai_premium_model: str = "gpt-4o" # used for premium/enterprise plan
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.2"       # any model you have pulled

    # Stripe
    stripe_secret_key: str
    stripe_webhook_secret: str
    stripe_price_basic: str
    stripe_price_pro: str
    stripe_price_premium: str

    # App
    app_env: str = "development"
    frontend_url: str = "http://localhost:5173"
    # Comma-separated production CORS origins. Defaults to FRONTEND_URL only.
    cors_origins: str = ""
    # Comma-separated hostnames accepted by TrustedHostMiddleware in production.
    trusted_hosts: str = ""
    api_secret: str | None = None          # not currently used; remove weak default
    # Development/demo only: lets testers rerun free-tier audits without monthly gating.
    demo_allow_unlimited_audits: bool = False
    audit_rate_limit: str = "5/hour"
    # Comma-separated Clerk user IDs granted admin access, e.g. "user_abc,user_xyz"
    admin_user_ids: str = ""
    # Optional: Clerk JWT audience claim.  If set, token `aud` is verified against this value.
    # Find it in your Clerk dashboard → JWT Templates → your template → Audience field.
    # Leave empty to skip audience verification (safe for default Clerk JWT templates).
    clerk_jwt_audience: str | None = None
    # In production we fail fast unless JWT audience verification is configured explicitly.
    clerk_require_audience_in_production: bool = True

    # Email (Resend) — optional; emails are silently skipped if not set
    resend_api_key: str | None = None
    from_email: str = "LBT OS <noreply@lbt-os.com>"

    # SMS (Twilio) — optional; SMS is silently skipped if not set
    twilio_account_sid: str | None = None
    twilio_auth_token: str | None = None
    twilio_from_number: str | None = None  # e.g. +15555550100

    # Integration / OAuth
    integration_secret_key: str | None = None
    quickbooks_client_id: str | None = None
    quickbooks_client_secret: str | None = None
    quickbooks_redirect_uri: str | None = None
    hubspot_client_id: str | None = None
    hubspot_client_secret: str | None = None
    hubspot_redirect_uri: str | None = None

    @property
    def should_verify_clerk_audience(self) -> bool:
        return bool(self.clerk_jwt_audience)

    @property
    def should_bypass_audit_monthly_limit(self) -> bool:
        return self.app_env.lower() != "production" and self.demo_allow_unlimited_audits

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() == "production"

    @property
    def parsed_cors_origins(self) -> list[str]:
        configured = [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]
        origins = configured or [self.frontend_url]
        if not self.is_production:
            origins.extend([
                "http://localhost:5173",
                "http://localhost:5174",
                "http://localhost:5175",
                "http://localhost:5176",
                "http://localhost:5177",
                "http://127.0.0.1:5173",
                "http://127.0.0.1:5174",
                "http://127.0.0.1:5175",
                "http://127.0.0.1:5176",
                "http://127.0.0.1:5177",
            ])
        return list(dict.fromkeys(origins))

    @property
    def parsed_trusted_hosts(self) -> list[str]:
        configured = [host.strip() for host in self.trusted_hosts.split(",") if host.strip()]
        if configured:
            return configured
        if self.is_production:
            raise ValueError("TRUSTED_HOSTS must be set when APP_ENV=production.")
        return ["localhost", "127.0.0.1", "*.localhost", "testserver"]

    def model_post_init(self, __context) -> None:
        if (
            self.app_env.lower() == "production"
            and self.clerk_require_audience_in_production
            and not self.clerk_jwt_audience
        ):
            raise ValueError(
                "CLERK_JWT_AUDIENCE must be set when APP_ENV=production. "
                "Set CLERK_REQUIRE_AUDIENCE_IN_PRODUCTION=false only if you have intentionally accepted that risk."
            )
        if self.app_env.lower() == "production":
            required_stripe = {
                "STRIPE_SECRET_KEY": self.stripe_secret_key,
                "STRIPE_WEBHOOK_SECRET": self.stripe_webhook_secret,
                "STRIPE_PRICE_BASIC": self.stripe_price_basic,
                "STRIPE_PRICE_PRO": self.stripe_price_pro,
                "STRIPE_PRICE_PREMIUM": self.stripe_price_premium,
            }
            missing = [name for name, value in required_stripe.items() if not value or "placeholder" in value.lower()]
            if missing:
                raise ValueError(f"Production Stripe configuration is incomplete: {', '.join(missing)}.")
            if not self.stripe_secret_key.startswith("sk_"):
                raise ValueError("STRIPE_SECRET_KEY must be a Stripe secret key in production.")
            if not self.stripe_webhook_secret.startswith("whsec_"):
                raise ValueError("STRIPE_WEBHOOK_SECRET must be a Stripe webhook signing secret in production.")
            bad_prices = [
                name for name in ("STRIPE_PRICE_BASIC", "STRIPE_PRICE_PRO", "STRIPE_PRICE_PREMIUM")
                if not required_stripe[name].startswith("price_")
            ]
            if bad_prices:
                raise ValueError(f"Stripe price IDs must start with price_: {', '.join(bad_prices)}.")


settings = Settings()

PLAN_PRICE_MAP: dict[str, str] = {
    "basic":   settings.stripe_price_basic,
    "pro":     settings.stripe_price_pro,
    "premium": settings.stripe_price_premium,
}

PLAN_FEATURES: dict[str, list[str]] = {
    "basic":   ["leads", "sales", "customers", "expenses", "dashboard"],
    "pro":     ["leads", "sales", "customers", "expenses", "dashboard", "ai_audit"],
    "premium": ["leads", "sales", "customers", "expenses", "dashboard", "ai_audit", "consulting"],
    "enterprise": ["leads", "sales", "customers", "expenses", "dashboard", "ai_audit", "consulting", "dots_video"],
}
