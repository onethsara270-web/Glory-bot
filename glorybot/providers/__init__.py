from glorybot.config import Settings
from glorybot.providers.base import ProviderAdapter
from glorybot.providers.glory_provider import GloryProvider
from glorybot.providers.mock_provider import MockProvider


def build_provider(s: Settings) -> ProviderAdapter:
    if s.provider_mode == "live":
        return GloryProvider(s.glory_api_base_url, s.glory_api_key.get_secret_value(),
                             s.glory_api_master_key.get_secret_value(), s.glory_api_timeout)
    return MockProvider()
