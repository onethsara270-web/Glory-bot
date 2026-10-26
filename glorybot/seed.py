import logging

from sqlalchemy import select

from glorybot.database.models import PricingConfiguration, Region
from glorybot.database.session import tx

log = logging.getLogger(__name__)

REGIONS = [("BR", "Brazil", "🇧🇷"), ("BD", "Bangladesh", "🇧🇩"), ("IN", "India", "🇮🇳"),
           ("ID", "Indonesia", "🇮🇩"), ("ME", "Middle East", "🌍"), ("NA", "North America", "🇺🇸"),
           ("PK", "Pakistan", "🇵🇰"), ("TH", "Thailand", "🇹🇭")]


async def seed(settings, provider) -> None:
    async with tx() as s:
        existing = {r.code: r for r in (await s.execute(select(Region))).scalars()}
        for i, (code, name, emoji) in enumerate(REGIONS):
            r = existing.get(code)
            if r is None:
                r = Region(code=code, display_name=name, emoji=emoji, sort_order=i, enabled=True, tier="basic")
                s.add(r)
            if provider.is_mock:
                r.provider_region_id = code.lower()
                r.tier = "premium" if code == "ME" else "basic"
        for cur, price in (("NPR", settings.default_price_per_squad),):
            has = (await s.execute(select(PricingConfiguration.id).where(
                PricingConfiguration.region_code.is_(None), PricingConfiguration.currency == cur).limit(1))).first()
            if not has:
                s.add(PricingConfiguration(region_code=None, price_per_squad=price, currency=cur))
    if not provider.is_mock:
        await sync_regions(provider)


async def sync_regions(provider) -> dict:
    """Map our 8 internal regions to the provider's region ids (and basic/premium tier) from /api/pricing."""
    try:
        remote = await provider.get_supported_regions()
    except Exception as e:  # network/provider problem must not stop the bot
        log.warning("Region sync failed: %s", type(e).__name__)
        return {}
    result = {}
    async with tx() as s:
        for r in (await s.execute(select(Region))).scalars():
            match = next((x for x in remote
                          if str(x.get("region_code", "")).upper() == r.code
                          or str(x.get("id", "")).lower() == r.code.lower()
                          or str(x.get("region_name", "")).lower() == r.display_name.lower()), None)
            r.provider_region_id = str(match["id"]) if match else None
            if match:
                r.tier = "premium" if str(match.get("tier", "basic")).lower() == "premium" else "basic"
            result[r.code] = r.provider_region_id
    log.info("Region mapping: %s", result)
    return result
