"""Adapter for the ffglory.pro reseller API, built strictly from the supplied documentation.

Auth: `x-api-key: <master key>` header AND `?api_key=<account key>` query param on every
call except GET /api/pricing (public).
Endpoints used: GET /api/auth/me, GET /api/pricing, POST /api/client/auto-clan-group,
GET /api/client/my-groups, POST /api/client/group-action.

The docs define no idempotency key. So create_order is never retried here; any ambiguous
result is returned as UNCERTAIN and resolved via list_groups() reconciliation.
"""
import logging

import httpx

from glorybot.providers.base import CreateOutcome, CreateResult, ProviderAdapter, ProviderGroup, extract_name

log = logging.getLogger(__name__)

SAFE_KEYS = {"success", "group_id", "bot_count", "error", "message", "balance", "basic_credits",
             "premium_credits", "status"}


def sanitize(data) -> dict:
    if isinstance(data, dict):
        return {k: v for k, v in data.items() if k in SAFE_KEYS}
    return {}


class GloryProvider(ProviderAdapter):
    is_mock = False

    def __init__(self, base_url: str, api_key: str, master_key: str, timeout: float = 20.0):
        self._key = api_key
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"x-api-key": master_key},
            timeout=httpx.Timeout(connect=5.0, read=timeout, write=10.0, pool=5.0),
        )

    async def _req(self, method: str, path: str, *, json=None, auth=True) -> httpx.Response:
        params = {"api_key": self._key} if auth else None
        return await self._client.request(method, path, params=params, json=json)

    async def get_account_balance(self) -> dict:
        r = await self._req("GET", "/api/auth/me")
        r.raise_for_status()
        d = r.json()
        return {"basic_credits": d.get("basic_credits"), "premium_credits": d.get("premium_credits"),
                "max_groups": d.get("max_groups")}

    async def get_supported_regions(self) -> list[dict]:
        r = await self._client.get("/api/pricing")  # public: no api_key
        r.raise_for_status()
        return [x for x in r.json().get("regions", []) if x.get("enabled")]

    async def create_order(self, *, provider_region: str, guild_uid: str) -> CreateResult:
        try:
            r = await self._req("POST", "/api/client/auto-clan-group",
                                json={"region": provider_region, "clan_id": guild_uid})
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            # TCP connection never established -> request cannot have been processed.
            return CreateResult(CreateOutcome.REJECTED, error_code="network",
                                message="Could not connect to provider")
        except httpx.HTTPError as e:
            # Sent (or maybe sent) but no usable response -> unknown. Never retry.
            return CreateResult(CreateOutcome.UNCERTAIN, error_code="network",
                                message=f"No response ({type(e).__name__})")

        try:
            body = r.json()
        except ValueError:
            body = {}
        raw = sanitize(body)

        if r.status_code == 200 and body.get("success") and body.get("group_id"):
            return CreateResult(CreateOutcome.CREATED, group_id=str(body["group_id"]),
                                http_status=200, raw=raw)

        codes = {402: "insufficient_credits", 403: "limit_reached", 409: "busy",
                 400: "bad_request", 401: "auth_failed", 429: "rate_limited"}
        if r.status_code in codes:
            return CreateResult(CreateOutcome.REJECTED, http_status=r.status_code,
                                error_code=codes[r.status_code],
                                message=str(body.get("error", ""))[:200], raw=raw)
        # 5xx, or 200 without a group_id: the provider may have started work.
        return CreateResult(CreateOutcome.UNCERTAIN, http_status=r.status_code, error_code="server",
                            message=str(body.get("error", ""))[:200], raw=raw)

    async def list_groups(self) -> list[ProviderGroup]:
        r = await self._req("GET", "/api/client/my-groups")
        r.raise_for_status()
        return [ProviderGroup(str(g["group_id"]), str(g.get("clan_id")), str(g.get("region")),
                              str(g.get("status")), g.get("created_at"), int(g.get("running_count") or 0),
                              int(g.get("container_count") or 0), g.get("uptime"), extract_name(g)) for g in r.json()]

    async def group_action(self, group_id: str, action: str) -> dict:
        if action not in {"restart", "stop", "delete", "get-glory", "get-clan-info"}:
            raise ValueError("unsupported action")
        r = await self._req("POST", "/api/client/group-action",
                            json={"action": action, "group_id": group_id})
        r.raise_for_status()
        return r.json()

    async def group_history(self) -> list[dict]:
        r = await self._req("GET", "/api/client/group-history")
        r.raise_for_status()
        return r.json()

    async def redeem_coupon(self, code: str) -> dict:
        """POST /api/client/redeem-coupon {code}. The provider allows 5 attempts per 60 seconds."""
        r = await self._req("POST", "/api/client/redeem-coupon", json={"code": code})
        try:
            body = r.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {"result": body}
        body["_ok"] = r.status_code == 200 and body.get("success", True) is not False
        body["_status"] = r.status_code
        return body

    async def notifications(self) -> list[dict]:
        r = await self._req("GET", "/api/client/notifications")  # provider clears them after reading
        r.raise_for_status()
        return r.json()

    async def aclose(self) -> None:
        await self._client.aclose()
