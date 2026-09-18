from supabase import Client, create_client

from .config import settings

# Single shared Supabase client (service role — bypasses RLS, safe on server)
# All queries MUST manually filter by org_id for tenant isolation.
_supabase: Client | None = None


def get_db() -> Client:
    global _supabase
    if _supabase is None:
        _supabase = create_client(settings.supabase_url, settings.supabase_service_key)
    return _supabase
