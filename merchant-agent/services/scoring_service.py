"""
Stage 5 of the recommendation pipeline: score and rank PurchaseOptions.

Rule-based, not ML -- no purchase-history data to train on, and a
transparent score is more defensible for the "every action explainable"
bar than a black-box model. Every signal is 0..1; final score is the
WEIGHTS-weighted sum (services/constants.py), and `reasons` on the option
already explain *why* it scored the way it did.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from db.models import Cart
from services.constants import COMPANION_CATEGORIES, LOW_STOCK_THRESHOLD, MAX_DISCOUNT_PERCENT, WEIGHTS
from services.purchase_options_service import BudgetStatus, OptionType, PurchaseOption


def _cart_product_names(session: Session, cart_id: str | None) -> set[str]:
    if not cart_id:
        return set()
    cart = session.get(Cart, cart_id)
    if not cart:
        return set()
    return {item.product.name for item in cart.items}


def score_options(
    session: Session, options: list[PurchaseOption], *, cart_id: str | None = None
) -> list[PurchaseOption]:
    cart_names = _cart_product_names(session, cart_id)
    companion_names: set[str] = set()
    for n in cart_names:
        companion_names |= COMPANION_CATEGORIES.get(n, set())

    for o in options:
        discount_score = min(o.savings_percent / MAX_DISCOUNT_PERCENT, 1.0)

        # value: reward options where every item is well-rated
        ratings = [_item_rating(session, i.product_id) for i in o.items]
        value_score = sum(ratings) / (5 * len(ratings)) if ratings else 0.0

        cross_sell_score = 0.0
        item_names = {i.name for i in o.items}
        if item_names & companion_names:
            cross_sell_score = 1.0
        elif o.option_type == OptionType.BUNDLE and len(item_names) > 1:
            cross_sell_score = 0.5  # bundle is itself a cross-sell even with no cart context

        urgency_score = 0.0
        for i in o.items:
            if _item_stock(session, i.product_id) <= LOW_STOCK_THRESHOLD:
                urgency_score = 1.0
                break

        budget_fit_score = 1.0 if o.budget_status == BudgetStatus.WITHIN else 0.5

        o.score = (
            WEIGHTS["match"] * o.match_score
            + WEIGHTS["budget_fit"] * budget_fit_score
            + WEIGHTS["discount"] * discount_score
            + WEIGHTS["value"] * value_score
            + WEIGHTS["cross_sell"] * cross_sell_score
            + WEIGHTS["urgency"] * urgency_score
        )

    options.sort(key=lambda o: o.score, reverse=True)
    return options


# small helpers -- avoid a second DB round trip per option by caching within
# a single score_options() call would be the next optimization; fine as-is
# at 480 SKUs / single-digit option counts per request.


def _item_rating(session: Session, product_id: str) -> int:
    from db.models import Product

    p = session.get(Product, product_id)
    return p.rating if p else 0


def _item_stock(session: Session, product_id: str) -> int:
    from db.models import Product

    p = session.get(Product, product_id)
    return p.inventory.stock_quantity if p and p.inventory else 0