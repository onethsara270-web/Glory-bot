from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum


class CreateOutcome(str, Enum):
    CREATED = "CREATED"      # provider confirmed; group_id present
    REJECTED = "REJECTED"    # provider definitively refused, nothing was created
    UNCERTAIN = "UNCERTAIN"  # request may have been processed; DO NOT retry, reconcile


@dataclass
class CreateResult:
    outcome: CreateOutcome
    group_id: str | None = None
    http_status: int | None = None
    error_code: str | None = None   # insufficient_credits | limit_reached | busy | network | server | ...
    message: str = ""
    raw: dict = field(default_factory=dict)  # already sanitized


_NAME_KEYS = ("clan_name", "guild_name", "clanName", "guildName", "name")


def extract_name(d, depth: int = 0) -> str | None:
    """Guild name from any provider answer. The docs don't show the exact shape, so look for the usual
    keys at the top level and a few levels down."""
    if not isinstance(d, dict) or depth > 2:
        return None
    for k in _NAME_KEYS:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    for k in ("data", "clan", "guild", "info", "result", "clan_info"):
        n = extract_name(d.get(k), depth + 1)
        if n:
            return n
    return None


@dataclass
class ProviderGroup:
    group_id: str
    clan_id: str
    region: str
    status: str
    created_at: str | None = None
    running_count: int = 0
    container_count: int = 0
    uptime: str | None = None
    name: str | None = None


class ProviderAdapter(ABC):
    is_mock: bool = False

    @abstractmethod
    async def get_account_balance(self) -> dict: ...

    @abstractmethod
    async def get_supported_regions(self) -> list[dict]: ...

    @abstractmethod
    async def create_order(self, *, provider_region: str, guild_uid: str) -> CreateResult:
        """Launch ONE group (one squad). Caller loops once per squad and records each result."""

    @abstractmethod
    async def list_groups(self) -> list[ProviderGroup]: ...

    @abstractmethod
    async def group_action(self, group_id: str, action: str) -> dict: ...

    async def group_history(self) -> list[dict]:
        raise NotImplementedError

    async def redeem_coupon(self, code: str) -> dict:
        raise NotImplementedError

    async def clan_info(self, group_id: str) -> dict:
        return await self.group_action(group_id, "get-clan-info")

    async def notifications(self) -> list[dict]:
        raise NotImplementedError

    async def aclose(self) -> None:
        return None
