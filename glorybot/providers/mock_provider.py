import itertools

from glorybot.providers.base import CreateOutcome, CreateResult, ProviderAdapter, ProviderGroup


class MockProvider(ProviderAdapter):
    """Deterministic provider for dev/tests. Set `script` to force outcomes in order."""
    is_mock = True

    def __init__(self, script: list[CreateOutcome] | None = None):
        self.script = list(script or [])
        self.calls: list[dict] = []
        self._ids = itertools.count(1)
        self.groups: list[ProviderGroup] = []
        self.credits = {"basic": 999, "premium": 999}
        self.tick = 0
        self.create_on_uncertain = False   # tests: an "unclear" answer that did create a group
        self.fail_listing = False          # tests: list_groups() unavailable
        self.coupons: dict[str, int] = {"MOCK10": 10}

    async def get_account_balance(self) -> dict:
        return {"basic_credits": self.credits["basic"], "premium_credits": self.credits["premium"], "max_groups": 999}

    async def get_supported_regions(self) -> list[dict]:
        return [{"id": c, "region_name": c, "tier": "premium" if c == "me" else "basic", "enabled": True}
                for c in ("br", "bd", "in", "id", "me", "na", "pk", "th")]

    async def create_order(self, *, provider_region: str, guild_uid: str) -> CreateResult:
        self.calls.append({"region": provider_region, "uid": guild_uid})
        item = self.script.pop(0) if self.script else CreateOutcome.CREATED
        outcome, code = item if isinstance(item, tuple) else (item, "mock")
        if outcome == CreateOutcome.CREATED or (outcome == CreateOutcome.UNCERTAIN and self.create_on_uncertain):
            if outcome != CreateOutcome.CREATED:
                self.groups.append(ProviderGroup(f"mock-{next(self._ids)}", guild_uid, provider_region, "running", None, 4, 4, "5m", f"Mock Guild {guild_uid}"))
                return CreateResult(outcome, error_code=code, message="scripted")
            self.credits["basic"] = max(0, self.credits["basic"] - 1)
            gid = f"mock-{next(self._ids)}"
            self.groups.append(ProviderGroup(gid, guild_uid, provider_region, "running", None, 4, 4, "5m", f"Mock Guild {guild_uid}"))
            return CreateResult(outcome, group_id=gid, http_status=200, raw={"mock": True})
        return CreateResult(outcome, error_code=code, message="scripted")

    async def list_groups(self) -> list[ProviderGroup]:
        if self.fail_listing:
            raise RuntimeError("listing unavailable")
        return list(self.groups)

    async def group_history(self) -> list[dict]:
        self.tick += 1  # glory "grows" every time it is read, so live views can be demoed
        return [{"group_id": g.group_id, "clan_id": g.clan_id, "region": g.region, "status": g.status,
                 "glory_farmed": 250 * (i + 1) + 15 * self.tick} for i, g in enumerate(self.groups)]

    async def redeem_coupon(self, code: str) -> dict:
        n = self.coupons.pop(code, None)
        if n is None:
            return {"success": False, "error": "Invalid or used code", "_ok": False, "_status": 400}
        self.credits["basic"] += n
        return {"success": True, "message": f"{n} credits added", "_ok": True, "_status": 200}

    async def notifications(self) -> list[dict]:
        return []

    async def group_action(self, group_id: str, action: str) -> dict:
        g = next((x for x in self.groups if x.group_id == group_id), None)
        if action == "get-clan-info":
            return {"success": True, "clan_name": f"Mock Guild {g.clan_id}" if g else "Mock Guild"}
        return {"success": True, "mock": True}
