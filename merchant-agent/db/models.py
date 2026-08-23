"""
Merchant database schema.

Design notes (per initial plan, section 3):
    Kaggle CSV -> normalization -> Merchant Database
        Products | Inventory | Carts | Reservations | Quotes
        Authorizations | Transactions

The buyer agent never touches this layer directly -- it only ever sees
MCP tool responses built on top of these tables (section 3 / section 11).

Products vs Inventory are deliberately split:
    Products   = catalog metadata (mostly static; admin-mutable only)
    Inventory  = live, mutable stock state (available/reserved/sold)
This mirrors section 7 (Inventory Lifecycle) needing its own row to
transition AVAILABLE -> RESERVED -> SOLD independent of catalog edits.
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


# --------------------------------------------------------------------------
# Catalog
# --------------------------------------------------------------------------


class Product(Base):
    """Authoritative catalog record. One row per sellable SKU."""

    __tablename__ = "products"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    sku: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    source_row_id: Mapped[str] = mapped_column(String(32), index=True)  # raw Kaggle "Product ID"

    name: Mapped[str] = mapped_column(String(200), index=True)
    category: Mapped[str] = mapped_column(String(100), index=True)
    description: Mapped[str] = mapped_column(Text)

    price: Mapped[float] = mapped_column(Float)  # INR, merchant list price
    currency: Mapped[str] = mapped_column(String(8), default="INR")

    warranty_period_years: Mapped[int] = mapped_column(Integer)
    dimensions_cm: Mapped[str] = mapped_column(String(50))
    manufacturing_date: Mapped[str] = mapped_column(String(20))
    expiration_date: Mapped[str] = mapped_column(String(20))
    tags: Mapped[str] = mapped_column(String(200))  # comma-separated, derived from real attributes
    variant: Mapped[str] = mapped_column(String(100))  # display form, e.g. "Green/Large"
    color: Mapped[str] = mapped_column(String(30))
    size: Mapped[str] = mapped_column(String(30))
    rating: Mapped[int] = mapped_column(Integer)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    inventory: Mapped["Inventory"] = relationship(back_populates="product", uselist=False)
    promotions: Mapped[list["Promotion"]] = relationship(back_populates="product")


class InventoryStatus(str, enum.Enum):
    AVAILABLE = "AVAILABLE"
    LOW_STOCK = "LOW_STOCK"
    OUT_OF_STOCK = "OUT_OF_STOCK"


class Inventory(Base):
    """Live, mutable stock state. Separate from Product so catalog edits
    (admin) never race with reservation/sale mutations (commerce core)."""

    __tablename__ = "inventory"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"), unique=True)

    stock_quantity: Mapped[int] = mapped_column(Integer)  # AVAILABLE units, unreserved
    reserved_quantity: Mapped[int] = mapped_column(Integer, default=0)
    sold_quantity: Mapped[int] = mapped_column(Integer, default=0)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    product: Mapped["Product"] = relationship(back_populates="inventory")

    @property
    def status(self) -> InventoryStatus:
        if self.stock_quantity <= 0:
            return InventoryStatus.OUT_OF_STOCK
        if self.stock_quantity <= 5:
            return InventoryStatus.LOW_STOCK
        return InventoryStatus.AVAILABLE


class Promotion(Base):
    """Merchant-controlled, time-bound discount on a product. Not part of
    the raw catalog data -- feeds get_recommendations (revenue-growth
    direction) rather than the core transact-safely pipeline."""

    __tablename__ = "promotions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"), index=True)
    discount_percent: Mapped[float] = mapped_column(Float)
    reason: Mapped[str] = mapped_column(String(200))  # e.g. "Overstock clearance"

    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    valid_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    product: Mapped["Product"] = relationship(back_populates="promotions")


# --------------------------------------------------------------------------
# Commerce core: carts, reservations, quotes
# --------------------------------------------------------------------------


class CartStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    COMPLETED = "COMPLETED"


class Cart(Base):
    __tablename__ = "carts"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    buyer_agent_id: Mapped[str] = mapped_column(String(100), index=True)
    status: Mapped[str] = mapped_column(String(20), default=CartStatus.ACTIVE.value)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    items: Mapped[list["CartItem"]] = relationship(back_populates="cart", cascade="all, delete-orphan")
    reservations: Mapped[list["Reservation"]] = relationship(back_populates="cart")
    quotes: Mapped[list["Quote"]] = relationship(back_populates="cart")


class CartItem(Base):
    __tablename__ = "cart_items"
    __table_args__ = (UniqueConstraint("cart_id", "product_id", name="uq_cart_product"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    cart_id: Mapped[str] = mapped_column(ForeignKey("carts.id"))
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"))
    quantity: Mapped[int] = mapped_column(Integer)
    unit_price_at_add: Mapped[float] = mapped_column(Float)  # merchant-fetched, never buyer-supplied

    cart: Mapped["Cart"] = relationship(back_populates="items")
    product: Mapped["Product"] = relationship()


class ReservationStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    RELEASED = "RELEASED"
    FINALIZED = "FINALIZED"  # converted to a sale
    EXPIRED = "EXPIRED"


class Reservation(Base):
    """Ties a quantity of a product to a specific cart for a TTL window.
    Prevents two buyer agents from both successfully buying the last unit
    (section 7)."""

    __tablename__ = "reservations"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    cart_id: Mapped[str] = mapped_column(ForeignKey("carts.id"))
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"))
    quantity: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default=ReservationStatus.ACTIVE.value)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    cart: Mapped["Cart"] = relationship(back_populates="reservations")
    product: Mapped["Product"] = relationship()


class QuoteStatus(str, enum.Enum):
    VALID = "VALID"
    EXPIRED = "EXPIRED"
    INVALIDATED = "INVALIDATED"  # cart changed after quote was issued
    CONSUMED = "CONSUMED"  # used by a successful authorization + payment


class Quote(Base):
    __tablename__ = "quotes"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    cart_id: Mapped[str] = mapped_column(ForeignKey("carts.id"))
    amount: Mapped[float] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(8), default="INR")
    status: Mapped[str] = mapped_column(String(20), default=QuoteStatus.VALID.value)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    cart: Mapped["Cart"] = relationship(back_populates="quotes")


# --------------------------------------------------------------------------
# Policy / delegation
# --------------------------------------------------------------------------


class PolicyDecision(str, enum.Enum):
    ALLOW = "ALLOW"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"
    BLOCK = "BLOCK"


class AuthorizationStatus(str, enum.Enum):
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    ALLOWED = "ALLOWED"  # granted directly, no human step needed
    BLOCKED = "BLOCKED"
    EXPIRED = "EXPIRED"
    EXECUTED = "EXECUTED"  # payment has run against this authorization (one-time use)


class Authorization(Base):
    """Bound to buyer_agent_id + cart_id + quote_id + amount + expiry.
    Cannot be reused for another agent/cart/quote/amount/time window
    (section 6, Authorization binding)."""

    __tablename__ = "authorizations"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    buyer_agent_id: Mapped[str] = mapped_column(String(100), index=True)
    cart_id: Mapped[str] = mapped_column(ForeignKey("carts.id"))
    quote_id: Mapped[str] = mapped_column(ForeignKey("quotes.id"))
    amount: Mapped[float] = mapped_column(Float)

    status: Mapped[str] = mapped_column(String(20), default=AuthorizationStatus.PENDING_APPROVAL.value)
    decision: Mapped[str] = mapped_column(String(20))  # PolicyDecision at creation time
    decision_reason: Mapped[str] = mapped_column(Text, default="")

    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True, default=_uuid)
    executed: Mapped[bool] = mapped_column(Boolean, default=False)  # replay guard

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ApprovalStatus(str, enum.Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class ApprovalRequest(Base):
    __tablename__ = "approval_requests"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    authorization_id: Mapped[str] = mapped_column(ForeignKey("authorizations.id"))
    status: Mapped[str] = mapped_column(String(20), default=ApprovalStatus.PENDING.value)
    reason: Mapped[str] = mapped_column(Text, default="")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# --------------------------------------------------------------------------
# Payment
# --------------------------------------------------------------------------


class TransactionStatus(str, enum.Enum):
    CREATED = "CREATED"
    PAYMENT_PENDING = "PAYMENT_PENDING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    authorization_id: Mapped[str] = mapped_column(ForeignKey("authorizations.id"))
    cart_id: Mapped[str] = mapped_column(ForeignKey("carts.id"))
    amount: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(20), default=TransactionStatus.CREATED.value)

    razorpay_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    razorpay_payment_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure_reason: Mapped[str] = mapped_column(Text, default="")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


# --------------------------------------------------------------------------
# Audit
# --------------------------------------------------------------------------


class AuditLayer(str, enum.Enum):
    AGENT = "AGENT"
    MCP = "MCP"
    COMMERCE = "COMMERCE"
    INVENTORY = "INVENTORY"
    POLICY = "POLICY"
    HUMAN = "HUMAN"
    PAYMENT = "PAYMENT"


class AuditEvent(Base):
    """Standard audit event (section 8.3). Every service emits through the
    one audit service (audit/service.py) instead of ad-hoc logging so every
    row here shares this exact shape."""

    __tablename__ = "audit_events"

    event_id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    trace_id: Mapped[str] = mapped_column(String(32), index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    layer: Mapped[str] = mapped_column(String(20))
    actor_type: Mapped[str] = mapped_column(String(20))  # AGENT | SYSTEM | HUMAN
    actor_id: Mapped[str] = mapped_column(String(100))

    action: Mapped[str] = mapped_column(String(100))
    resource_type: Mapped[str] = mapped_column(String(50))
    resource_id: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(30))

    decision: Mapped[str | None] = mapped_column(String(30), nullable=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    state_before: Mapped[str] = mapped_column(Text, default="")  # JSON string
    state_after: Mapped[str] = mapped_column(Text, default="")  # JSON string
    event_metadata: Mapped[str] = mapped_column(Text, default="")  # JSON string