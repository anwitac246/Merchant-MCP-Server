"""
SQLAlchemy models for the merchant MCP server.

Layers, per the project plan:
  - Product / Inventory / Promotion   -> catalog + stock (used by normalize.py today)
  - Cart / CartItem / Reservation / Quote        -> commerce core (no service layer yet)
  - Authorization / ApprovalRequest              -> policy/delegation (no service layer yet)
  - Transaction                                   -> Razorpay bridge (no service layer yet)
  - AuditEvent                                    -> audit trail (nothing writes to it yet)
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# Catalog + stock
# --------------------------------------------------------------------------


class Product(Base):
    __tablename__ = "products"

    id = Column(Integer, primary_key=True)
    sku = Column(String, unique=True, nullable=False, index=True)
    source_row_id = Column(String, nullable=True)  # original asin/row id from ingestion

    name = Column(String, nullable=False)
    category = Column(String, nullable=False, index=True)
    description = Column(Text, nullable=True)

    price = Column(Float, nullable=False)  # INR, list price before promotions
    currency = Column(String, default="INR", nullable=False)

    warranty_period_years = Column(Integer, nullable=True)
    dimensions_cm = Column(String, nullable=True)
    manufacturing_date = Column(String, nullable=True)
    expiration_date = Column(String, nullable=True)

    tags = Column(Text, nullable=True)  # csv string, e.g. "electronics,samsung,blue,bestseller"
    brand = Column(String, nullable=True, index=True)
    variant = Column(String, nullable=True)
    color = Column(String, nullable=True, index=True)
    size = Column(String, nullable=True)
    rating = Column(Integer, nullable=True)  # 1..5

    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=_utcnow, nullable=False)

    inventory = relationship("Inventory", back_populates="product", uselist=False)
    promotions = relationship("Promotion", back_populates="product")

    def tag_list(self) -> list[str]:
        return [t for t in (self.tags or "").split(",") if t]


class Inventory(Base):
    """Deliberately separate from Product so catalog edits never race with
    reservation/sale mutations."""

    __tablename__ = "inventory"

    id = Column(Integer, primary_key=True)
    product_id = Column(Integer, ForeignKey("products.id"), unique=True, nullable=False)

    stock_quantity = Column(Integer, default=0, nullable=False)
    reserved_quantity = Column(Integer, default=0, nullable=False)
    sold_quantity = Column(Integer, default=0, nullable=False)

    product = relationship("Product", back_populates="inventory")

    @property
    def available_quantity(self) -> int:
        return max(0, self.stock_quantity - self.reserved_quantity)


class Promotion(Base):
    """Merchant-side discount on a product."""

    __tablename__ = "promotions"

    id = Column(Integer, primary_key=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False, index=True)

    discount_percent = Column(Float, nullable=False)
    reason = Column(String, nullable=True)

    valid_from = Column(DateTime, nullable=False)
    valid_until = Column(DateTime, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)

    product = relationship("Product", back_populates="promotions")

    def is_currently_valid(self, at: datetime | None = None) -> bool:
        at = at or _utcnow()
        vf, vu = self.valid_from, self.valid_until
        if vf.tzinfo is None:
            vf = vf.replace(tzinfo=timezone.utc)
        if vu.tzinfo is None:
            vu = vu.replace(tzinfo=timezone.utc)
        return bool(self.is_active and vf <= at <= vu)


# --------------------------------------------------------------------------
# Commerce core (models only -- no service layer, no MCP tools yet)
# --------------------------------------------------------------------------


class Cart(Base):
    __tablename__ = "carts"

    id = Column(Integer, primary_key=True)
    buyer_ref = Column(String, nullable=True)  # opaque id for the buyer agent/session
    status = Column(String, default="open", nullable=False)  # open|checked_out|cancelled
    created_at = Column(DateTime, default=_utcnow, nullable=False)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow, nullable=False)

    items = relationship("CartItem", back_populates="cart")


class CartItem(Base):
    __tablename__ = "cart_items"

    id = Column(Integer, primary_key=True)
    cart_id = Column(Integer, ForeignKey("carts.id"), nullable=False, index=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)

    quantity = Column(Integer, default=1, nullable=False)
    unit_price_snapshot = Column(Float, nullable=True)  # price at time of add

    cart = relationship("Cart", back_populates="items")
    product = relationship("Product")


class Reservation(Base):
    """Temporary stock hold tied to a cart item while checkout is in flight."""

    __tablename__ = "reservations"

    id = Column(Integer, primary_key=True)
    cart_item_id = Column(Integer, ForeignKey("cart_items.id"), nullable=False)
    quantity = Column(Integer, nullable=False)
    status = Column(String, default="active", nullable=False)  # active|released|consumed
    created_at = Column(DateTime, default=_utcnow, nullable=False)
    expires_at = Column(DateTime, nullable=True)


class Quote(Base):
    """A priced-and-frozen snapshot of a cart, handed to the buyer agent to accept."""

    __tablename__ = "quotes"

    id = Column(Integer, primary_key=True)
    cart_id = Column(Integer, ForeignKey("carts.id"), nullable=False)

    subtotal = Column(Float, nullable=False)
    discount_total = Column(Float, default=0.0, nullable=False)
    tax_total = Column(Float, default=0.0, nullable=False)
    total = Column(Float, nullable=False)
    currency = Column(String, default="INR", nullable=False)

    created_at = Column(DateTime, default=_utcnow, nullable=False)
    expires_at = Column(DateTime, nullable=True)


# --------------------------------------------------------------------------
# Policy / delegation (models only)
# --------------------------------------------------------------------------


class Authorization(Base):
    """A buyer-agent's delegated spending authority (e.g. from AP2/ACP-style mandate)."""

    __tablename__ = "authorizations"

    id = Column(Integer, primary_key=True)
    buyer_ref = Column(String, nullable=False)
    max_amount = Column(Float, nullable=False)
    currency = Column(String, default="INR", nullable=False)
    scope = Column(Text, nullable=True)  # json-encoded scope/constraints
    status = Column(String, default="active", nullable=False)  # active|revoked|expired
    created_at = Column(DateTime, default=_utcnow, nullable=False)
    expires_at = Column(DateTime, nullable=True)


class ApprovalRequest(Base):
    """Human-in-the-loop gate for actions that exceed policy (e.g. above max_amount)."""

    __tablename__ = "approval_requests"

    id = Column(Integer, primary_key=True)
    trace_id = Column(String, nullable=True, index=True)
    related_entity = Column(String, nullable=True)  # e.g. "quote:42"
    reason = Column(Text, nullable=True)
    status = Column(String, default="pending", nullable=False)  # pending|approved|rejected
    requested_by = Column(String, nullable=True)
    decided_by = Column(String, nullable=True)
    created_at = Column(DateTime, default=_utcnow, nullable=False)
    decided_at = Column(DateTime, nullable=True)


# --------------------------------------------------------------------------
# Razorpay bridge (model only)
# --------------------------------------------------------------------------


class Transaction(Base):
    __tablename__ = "transactions"

    id = Column(Integer, primary_key=True)
    cart_id = Column(Integer, ForeignKey("carts.id"), nullable=True)
    razorpay_order_id = Column(String, nullable=True)
    razorpay_payment_id = Column(String, nullable=True)
    amount = Column(Float, nullable=False)
    currency = Column(String, default="INR", nullable=False)
    status = Column(String, default="created", nullable=False)
    created_at = Column(DateTime, default=_utcnow, nullable=False)


# --------------------------------------------------------------------------
# Audit trail (model only -- nothing writes to this yet)
# --------------------------------------------------------------------------


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id = Column(Integer, primary_key=True)
    trace_id = Column(String, nullable=False, index=True)
    layer = Column(String, nullable=False)  # AGENT|MCP|COMMERCE|INVENTORY|POLICY|HUMAN|PAYMENT
    event_type = Column(String, nullable=False)
    payload = Column(Text, nullable=True)  # json-encoded
    created_at = Column(DateTime, default=_utcnow, nullable=False, index=True)