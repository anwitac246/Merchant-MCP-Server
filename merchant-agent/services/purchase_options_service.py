"""
Stages 2-4 of the recommendation pipeline.

Stage 2: apply live commerce data (inventory, active discounts, bundles/
         group offers) to the candidates retrieval_service found.
Stage 3: package everything into a uniform PurchaseOption shape so SINGLE /
         DISCOUNTED / ALTERNATIVE / BUNDLE / GROUP are all comparable and
         sortable the same way in stage 5.
Stage 4: filter by budget (within / stretch / rejected-and-dropped).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from sqlalchemy.orm import Session

from db.models import Inventory, Product
from services.constants import BUNDLE_DISCOUNT_PCT, COMPANION_CATEGORIES, GROUP_TIERS, STRETCH_PCT
from services.retrieval_service import Candidate


class OptionType(str, Enum):
    SINGLE = "SINGLE"
    DISCOUNTED = "DISCOUNTED"
    ALTERNATIVE = "ALTERNATIVE"
    BUNDLE = "BUNDLE"
    GROUP = "GROUP"


class BudgetStatus(str, Enum):
    WITHIN = "WITHIN"
    STRETCH = "STRETCH"


@dataclass
class OptionItem:
    product_id: str
    sku: str
    name: str
    qty: int
    unit_price: float


@dataclass
class PurchaseOption:
    option_type: OptionType
    items: list[OptionItem]
    list_price: float  # pre-discount total
    total_price: float  # actual payable total
    match_score: float
    reasons: list[str] = field(default_factory=list)
    budget_status: BudgetStatus | None = None  # set in stage 4
    score: float = 0.0  # set in stage 5

    @property
    def savings(self) -> float:
        return round(self.list_price - self.total_price, 2)

    @property
    def savings_percent(self) -> float:
        if self.list_price <= 0:
            return 0.0
        return round(100 * self.savings / self.list_price, 1)

    @property
    def primary_product_id(self) -> str:
        return self.items[0].product_id


# --------------------------------------------------------------------------
# Stage 2: attach live commerce data, drop what can't actually be bought
# --------------------------------------------------------------------------


@dataclass
class CommerceCandidate:
    candidate: Candidate
    inventory: Inventory
    active_promo_discount: float  # 0 if none


def attach_commerce_data(session: Session, candidates: list[Candidate]) -> list[CommerceCandidate]:
    # SQLite drops tzinfo on round-trip, so promo.valid_from/valid_until come
    # back naive even though they were written as tz-aware -- compare naive
    # to naive rather than fight SQLite's storage behavior.
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    out: list[CommerceCandidate] = []
    for c in candidates:
        inv = c.product.inventory
        if not inv or inv.stock_quantity <= 0:
            continue  # out of stock -> not purchasable, drop entirely
        promo = next(
            (
                p
                for p in c.product.promotions
                if p.is_active and p.valid_from <= now <= p.valid_until
            ),
            None,
        )
        out.append(
            CommerceCandidate(
                candidate=c,
                inventory=inv,
                active_promo_discount=promo.discount_percent if promo else 0.0,
            )
        )
    return out


# --------------------------------------------------------------------------
# Stage 3: build purchase options
# --------------------------------------------------------------------------


def _item(p: Product, qty: int, unit_price: float) -> OptionItem:
    return OptionItem(product_id=p.id, sku=p.sku, name=p.name, qty=qty, unit_price=unit_price)


def _best_companion(
    session: Session, product: Product, exclude_ids: set[str]
) -> tuple[Product, Inventory] | None:
    companion_names = COMPANION_CATEGORIES.get(product.name, set())
    if not companion_names:
        return None
    candidates = (
        session.query(Product)
        .join(Inventory)
        .filter(Product.name.in_(companion_names))
        .filter(Product.is_active.is_(True))
        .filter(Inventory.stock_quantity > 0)
        .filter(~Product.id.in_(exclude_ids))
        .order_by(Product.rating.desc())
        .first()
    )
    if not candidates:
        return None
    return candidates, candidates.inventory


def _best_group_tier(stock_quantity: int) -> tuple[int, float] | None:
    for min_qty, discount in GROUP_TIERS:  # already ordered highest qty first
        if stock_quantity >= min_qty:
            return min_qty, discount
    return None


def build_purchase_options(
    session: Session, commerce_candidates: list[CommerceCandidate]
) -> list[PurchaseOption]:
    options: list[PurchaseOption] = []
    all_ids = {cc.candidate.product.id for cc in commerce_candidates}

    for cc in commerce_candidates:
        p = cc.candidate.product
        match_score = cc.candidate.match_score

        # -- SINGLE: product alone at list price -------------------------
        options.append(
            PurchaseOption(
                option_type=OptionType.SINGLE,
                items=[_item(p, 1, p.price)],
                list_price=p.price,
                total_price=p.price,
                match_score=match_score,
                reasons=[f"{p.name} ({p.variant})"],
            )
        )

        # -- DISCOUNTED: active promo applied -----------------------------
        if cc.active_promo_discount > 0:
            discounted = round(p.price * (1 - cc.active_promo_discount / 100), 2)
            options.append(
                PurchaseOption(
                    option_type=OptionType.DISCOUNTED,
                    items=[_item(p, 1, discounted)],
                    list_price=p.price,
                    total_price=discounted,
                    match_score=match_score,
                    reasons=[f"{cc.active_promo_discount:.0f}% off — overstock clearance"],
                )
            )

        # -- BUNDLE: product + best in-stock companion --------------------
        companion = _best_companion(session, p, exclude_ids=all_ids | {p.id})
        if companion:
            comp_product, comp_inv = companion
            list_total = p.price + comp_product.price
            bundle_total = round(list_total * (1 - BUNDLE_DISCOUNT_PCT / 100), 2)
            options.append(
                PurchaseOption(
                    option_type=OptionType.BUNDLE,
                    items=[_item(p, 1, p.price), _item(comp_product, 1, comp_product.price)],
                    list_price=list_total,
                    total_price=bundle_total,
                    match_score=match_score,
                    reasons=[
                        f"Bundle: {p.name} + {comp_product.name} — "
                        f"{BUNDLE_DISCOUNT_PCT:.0f}% off combined price"
                    ],
                )
            )

        # -- GROUP: quantity discount on the same product ------------------
        tier = _best_group_tier(cc.inventory.stock_quantity)
        if tier:
            qty, discount = tier
            list_total = p.price * qty
            group_total = round(list_total * (1 - discount / 100), 2)
            options.append(
                PurchaseOption(
                    option_type=OptionType.GROUP,
                    items=[_item(p, qty, p.price)],
                    list_price=list_total,
                    total_price=group_total,
                    match_score=match_score,
                    reasons=[f"Buy {qty}: {discount:.0f}% off"],
                )
            )

    # -- ALTERNATIVE: cheapest same-category candidate cheaper than the
    # cheapest exact match. One alternative offered, not one per exact
    # match -- otherwise every exact match resolves to the same cheapest
    # product and floods the list with near-duplicates. -------------------
    exact = [cc for cc in commerce_candidates if cc.candidate.match_type == "exact"]
    if exact:
        cheapest_exact = min(cc.candidate.product.price for cc in exact)
        cheaper_pool = [
            cc
            for cc in commerce_candidates
            if cc.candidate.product.category == exact[0].candidate.product.category
            and cc.candidate.product.price < cheapest_exact
        ]
        if cheaper_pool:
            best_alt = min(cheaper_pool, key=lambda o: o.candidate.product.price)
            alt_p = best_alt.candidate.product
            options.append(
                PurchaseOption(
                    option_type=OptionType.ALTERNATIVE,
                    items=[_item(alt_p, 1, alt_p.price)],
                    list_price=alt_p.price,
                    total_price=alt_p.price,
                    match_score=best_alt.candidate.match_score,
                    reasons=[f"Cheaper alternative ({alt_p.name}, {alt_p.variant})"],
                )
            )

    return options


# --------------------------------------------------------------------------
# Stage 4: filter by budget
# --------------------------------------------------------------------------


def apply_budget_filter(
    options: list[PurchaseOption], budget: float | None, stretch_pct: float = STRETCH_PCT
) -> list[PurchaseOption]:
    if budget is None:
        for o in options:
            o.budget_status = BudgetStatus.WITHIN
        return options

    stretch_ceiling = budget * (1 + stretch_pct)
    kept: list[PurchaseOption] = []
    for o in options:
        if o.total_price <= budget:
            o.budget_status = BudgetStatus.WITHIN
            kept.append(o)
        elif o.total_price <= stretch_ceiling:
            o.budget_status = BudgetStatus.STRETCH
            o.reasons.append(f"₹{o.total_price - budget:.0f} over budget")
            kept.append(o)
        # else: too expensive -> dropped, not returned
    return kept