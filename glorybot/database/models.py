import enum
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import (JSON, BigInteger, Boolean, CheckConstraint, Date, DateTime, Enum, ForeignKey,
                        Integer, Numeric, String, Text, UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from glorybot.database.base import Base

MONEY = Numeric(14, 2)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class OrderStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    PENDING_PAYMENT = "PENDING_PAYMENT"
    PENDING_ADMIN_APPROVAL = "PENDING_ADMIN_APPROVAL"
    APPROVED = "APPROVED"
    PROCESSING = "PROCESSING"
    SUBMITTED_TO_PROVIDER = "SUBMITTED_TO_PROVIDER"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    REQUIRES_RECONCILIATION = "REQUIRES_RECONCILIATION"
    REFUNDED = "REFUNDED"


class TxType(str, enum.Enum):
    DEPOSIT = "DEPOSIT"
    RESERVE = "RESERVE"
    RELEASE = "RELEASE"
    SPEND = "SPEND"
    REFUND = "REFUND"
    ADJUSTMENT = "ADJUSTMENT"
    WHEEL_PRIZE = "WHEEL_PRIZE"


class ProviderRequestStatus(str, enum.Enum):
    PENDING = "PENDING"        # row created, request not yet sent
    CREATED = "CREATED"        # provider confirmed a group
    REJECTED = "REJECTED"      # provider definitively refused; nothing created
    UNCERTAIN = "UNCERTAIN"    # outcome unknown; needs reconciliation


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class User(Base, TimestampMixin):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    telegram_username: Mapped[str | None] = mapped_column(String(64))
    login_username: Mapped[str | None] = mapped_column(String(20))
    login_username_lower: Mapped[str | None] = mapped_column(String(20), unique=True)
    display_name: Mapped[str | None] = mapped_column(String(40))
    country: Mapped[str | None] = mapped_column(String(32))
    currency: Mapped[str] = mapped_column(String(8), default="NPR")  # NPR | USDT
    avatar_file_id: Mapped[str | None] = mapped_column(String(256))
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE")  # ACTIVE | SUSPENDED
    language: Mapped[str] = mapped_column(String(5), default="en")
    referral_code: Mapped[str | None] = mapped_column(String(16), unique=True)
    total_squads: Mapped[int] = mapped_column(Integer, default=0)
    total_spent: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    total_successful_orders: Mapped[int] = mapped_column(Integer, default=0)
    admin_notes: Mapped[str | None] = mapped_column(Text)
    wallet: Mapped["WalletAccount"] = relationship(back_populates="user", uselist=False)


class UserCredential(Base, TimestampMixin):
    __tablename__ = "user_credentials"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    must_change: Mapped[bool] = mapped_column(Boolean, default=True)
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Admin(Base, TimestampMixin):
    __tablename__ = "admins"
    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    role: Mapped[str] = mapped_column(String(16), default="ADMIN")  # ADMIN | SUPER_ADMIN
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Region(Base, TimestampMixin):
    __tablename__ = "regions"
    code: Mapped[str] = mapped_column(String(16), primary_key=True)  # internal stable id e.g. "BR"
    display_name: Mapped[str] = mapped_column(String(48))
    emoji: Mapped[str] = mapped_column(String(8), default="🌍")
    provider_region_id: Mapped[str | None] = mapped_column(String(16))  # e.g. "me", "bd" from /api/pricing
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    tier: Mapped[str] = mapped_column(String(12), default="basic")  # basic | premium (which credit it uses)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class PricingConfiguration(Base):
    __tablename__ = "pricing_configurations"
    id: Mapped[int] = mapped_column(primary_key=True)
    region_code: Mapped[str | None] = mapped_column(ForeignKey("regions.code"))  # NULL = global default
    price_per_squad: Mapped[Decimal] = mapped_column(MONEY)
    currency: Mapped[str] = mapped_column(String(8), default="NPR")
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    set_by_admin: Mapped[int | None] = mapped_column(BigInteger)
    __table_args__ = (CheckConstraint("price_per_squad > 0"),)


class Order(Base, TimestampMixin):
    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_number: Mapped[str] = mapped_column(String(24), unique=True)  # GP-2026-000001
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    region_code: Mapped[str] = mapped_column(ForeignKey("regions.code"))
    guild_uid: Mapped[str] = mapped_column(String(32), index=True)
    squads: Mapped[int] = mapped_column(Integer)
    unit_price: Mapped[Decimal] = mapped_column(MONEY)   # snapshot at submit time
    total_amount: Mapped[Decimal] = mapped_column(MONEY)
    currency: Mapped[str] = mapped_column(String(8), default="NPR")
    status: Mapped[OrderStatus] = mapped_column(Enum(OrderStatus), default=OrderStatus.DRAFT, index=True)
    is_mock: Mapped[bool] = mapped_column(Boolean, default=False)
    settings_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    admin_id: Mapped[int | None] = mapped_column(BigInteger)
    reject_reason: Mapped[str | None] = mapped_column(Text)
    guild_name: Mapped[str | None] = mapped_column(String(80))
    placed_squads: Mapped[int | None] = mapped_column(Integer)  # squads actually started at the provider
    ordered_squads: Mapped[int | None] = mapped_column(Integer)  # what the customer asked for (squads may shrink on partial fills)
    payment_method: Mapped[str | None] = mapped_column(String(12))  # QR | WALLET
    payment_remark: Mapped[str | None] = mapped_column(String(24))
    proof_file_id: Mapped[str | None] = mapped_column(String(256))
    proof_unique_id: Mapped[str | None] = mapped_column(String(64), index=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    history: Mapped[list["OrderStatusHistory"]] = relationship(back_populates="order")
    __table_args__ = (CheckConstraint("squads >= 1 AND squads <= 10"), CheckConstraint("total_amount > 0"))


class OrderStatusHistory(Base):
    __tablename__ = "order_status_history"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    old_status: Mapped[str | None] = mapped_column(String(32))
    new_status: Mapped[str] = mapped_column(String(32))
    actor: Mapped[str] = mapped_column(String(64))  # "user:123" | "admin:456" | "system"
    reason: Mapped[str | None] = mapped_column(Text)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    order: Mapped[Order] = relationship(back_populates="history")


class WalletAccount(Base, TimestampMixin):
    __tablename__ = "wallet_accounts"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    available: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    reserved: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    lifetime_deposits: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    lifetime_spent: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    user: Mapped[User] = relationship(back_populates="wallet")
    __table_args__ = (CheckConstraint("available >= 0"), CheckConstraint("reserved >= 0"))


class Wallet(Base, TimestampMixin):
    """One wallet per user per currency (NPR / USDT). Replaces the old single-currency WalletAccount."""
    __tablename__ = "wallets"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    currency: Mapped[str] = mapped_column(String(8), default="NPR")
    available: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    reserved: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    lifetime_deposits: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    lifetime_spent: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    __table_args__ = (UniqueConstraint("user_id", "currency", name="uq_wallet_user_currency"),
                      CheckConstraint("available >= 0"), CheckConstraint("reserved >= 0"))


class WalletTransaction(Base):
    """Append-only ledger. Never update or delete rows."""
    __tablename__ = "wallet_transactions"
    id: Mapped[int] = mapped_column(primary_key=True)
    txn_id: Mapped[str] = mapped_column(String(32), unique=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    type: Mapped[TxType] = mapped_column(Enum(TxType))
    currency: Mapped[str | None] = mapped_column(String(8))
    amount: Mapped[Decimal] = mapped_column(MONEY)  # always positive; type gives direction
    available_after: Mapped[Decimal] = mapped_column(MONEY)
    reserved_after: Mapped[Decimal] = mapped_column(MONEY)
    ref_type: Mapped[str | None] = mapped_column(String(24))
    ref_id: Mapped[str | None] = mapped_column(String(32))
    reason: Mapped[str | None] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(String(64), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # Exactly-once guard: one RESERVE/RELEASE/REFUND/DEPOSIT/WHEEL_PRIZE per (type, ref)
    __table_args__ = (UniqueConstraint("type", "ref_type", "ref_id", name="uq_wallet_tx_once"),)


class DepositRequest(Base, TimestampMixin):
    __tablename__ = "deposit_requests"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    amount: Mapped[Decimal] = mapped_column(MONEY)
    payment_reference: Mapped[str] = mapped_column(String(128))
    proof_file_id: Mapped[str | None] = mapped_column(String(256))
    status: Mapped[str] = mapped_column(String(12), default="PENDING")  # PENDING|APPROVED|REJECTED
    admin_id: Mapped[int | None] = mapped_column(BigInteger)
    admin_note: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (UniqueConstraint("payment_reference", name="uq_deposit_reference"),)


class PaymentIntent(Base, TimestampMixin):
    """One QR payment attempt. Amount and unit price are frozen here at creation."""
    __tablename__ = "payment_intents"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    identifier: Mapped[str] = mapped_column(String(40), unique=True)
    provider_payment_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    amount: Mapped[Decimal] = mapped_column(MONEY)
    amount_paisa: Mapped[int] = mapped_column(Integer)
    unit_price: Mapped[Decimal] = mapped_column(MONEY)
    region_code: Mapped[str | None] = mapped_column(String(16))
    guild_uid: Mapped[str | None] = mapped_column(String(32))
    squads: Mapped[int] = mapped_column(Integer, default=0)
    currency: Mapped[str] = mapped_column(String(8), default="NPR")
    kind: Mapped[str] = mapped_column(String(8), default="ORDER")  # ORDER | TOPUP
    qr_url: Mapped[str | None] = mapped_column(Text)
    # PENDING | PAID | CREDITED (paid but order impossible -> wallet) | FAILED | CLOSED | MISMATCH | REVIEW
    status: Mapped[str] = mapped_column(String(12), default="PENDING", index=True)
    is_mock: Mapped[bool] = mapped_column(Boolean, default=False)
    order_id: Mapped[int | None] = mapped_column(Integer)
    qr_data: Mapped[str | None] = mapped_column(Text)
    qr_string: Mapped[str | None] = mapped_column(Text)
    checkout_url: Mapped[str | None] = mapped_column(Text)
    qr_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    qr_message_id: Mapped[int | None] = mapped_column(BigInteger)
    qr_is_photo: Mapped[bool] = mapped_column(Boolean, default=True)
    ui_open: Mapped[bool] = mapped_column(Boolean, default=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    note: Mapped[str | None] = mapped_column(Text)


class RefundRecord(Base):
    __tablename__ = "refund_records"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), unique=True)  # one refund per order
    amount: Mapped[Decimal] = mapped_column(MONEY)
    reason: Mapped[str] = mapped_column(Text)
    admin_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ProviderRequest(Base, TimestampMixin):
    """One row per squad = one provider 'launch group' call. The unique key is our local
    idempotency guard: the provider API offers no idempotency key, so we must never send
    a row that has already left PENDING."""
    __tablename__ = "provider_requests"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    squad_index: Mapped[int] = mapped_column(Integer)
    status: Mapped[ProviderRequestStatus] = mapped_column(
        Enum(ProviderRequestStatus), default=ProviderRequestStatus.PENDING)
    provider_group_id: Mapped[str | None] = mapped_column(String(64), index=True)
    http_status: Mapped[int | None] = mapped_column(Integer)
    sanitized_response: Mapped[dict | None] = mapped_column(JSON)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (UniqueConstraint("order_id", "squad_index", name="uq_provider_req_squad"),)


class ProviderWebhookEvent(Base):
    __tablename__ = "provider_webhook_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[str] = mapped_column(String(64), unique=True)
    payload: Mapped[dict] = mapped_column(JSON)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    processed: Mapped[bool] = mapped_column(Boolean, default=False)


class LuckyWheelConfiguration(Base):
    __tablename__ = "lucky_wheel_config"
    id: Mapped[int] = mapped_column(primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)  # disabled until owner approves
    daily_budget: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    # [{"key":"TRY_AGAIN","reward":0,"weight":10}, {"key":"NPR_1","reward":1,"weight":..}, ...]
    prizes: Mapped[list] = mapped_column(JSON, default=list)
    updated_by: Mapped[int | None] = mapped_column(BigInteger)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class LuckyWheelSpin(Base):
    __tablename__ = "lucky_wheel_spins"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    spin_date: Mapped[date] = mapped_column(Date)  # Kathmandu local date
    prize_key: Mapped[str] = mapped_column(String(16))
    reward: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (UniqueConstraint("user_id", "spin_date", name="uq_one_spin_per_day"),)


class SupportTicket(Base, TimestampMixin):
    __tablename__ = "support_tickets"
    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_no: Mapped[str] = mapped_column(String(16), unique=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    category: Mapped[str] = mapped_column(String(24))
    status: Mapped[str] = mapped_column(String(12), default="OPEN")


class SupportMessage(Base):
    __tablename__ = "support_messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("support_tickets.id"), index=True)
    sender: Mapped[str] = mapped_column(String(16))  # user | admin
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Broadcast(Base, TimestampMixin):
    __tablename__ = "broadcasts"
    id: Mapped[int] = mapped_column(primary_key=True)
    admin_id: Mapped[int] = mapped_column(BigInteger)
    audience: Mapped[str] = mapped_column(String(24))
    source_chat_id: Mapped[int] = mapped_column(BigInteger)
    source_message_id: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(12), default="PREVIEW")
    sent: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    blocked: Mapped[int] = mapped_column(Integer, default=0)


class BroadcastDelivery(Base):
    __tablename__ = "broadcast_deliveries"
    id: Mapped[int] = mapped_column(primary_key=True)
    broadcast_id: Mapped[int] = mapped_column(ForeignKey("broadcasts.id"), index=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(12))
    error: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (UniqueConstraint("broadcast_id", "telegram_id"),)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    actor_id: Mapped[int] = mapped_column(BigInteger, index=True)
    action: Mapped[str] = mapped_column(String(48))
    target: Mapped[str | None] = mapped_column(String(64))
    reason: Mapped[str | None] = mapped_column(Text)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class BotConfiguration(Base):
    __tablename__ = "bot_configuration"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class WebSession(Base):
    """Website login session. Only a hash of the cookie token is stored."""
    __tablename__ = "web_sessions"
    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class WebLoginCode(Base):
    """One-time code from the Telegram bot (/weblogin) that logs the same account into the website."""
    __tablename__ = "web_login_codes"
    id: Mapped[int] = mapped_column(primary_key=True)
    code_hash: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used: Mapped[bool] = mapped_column(Boolean, default=False)
