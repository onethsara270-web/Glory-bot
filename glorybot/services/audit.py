from glorybot.database.models import AuditLog


async def audit(s, actor_id: int, action: str, target: str | None = None, reason: str | None = None) -> None:
    s.add(AuditLog(actor_id=actor_id, action=action, target=target, reason=reason))
