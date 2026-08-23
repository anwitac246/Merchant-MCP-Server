"""
Recommendation pipeline orchestrator:

    Buyer Intent + Budget
        -> retrieval_service.retrieve                      (stage 1: candidates)
        -> purchase_options_service.attach_commerce_data    (stage 2)
        -> purchase_options_service.build_purchase_options  (stage 2-3)
        -> purchase_options_service.apply_budget_filter     (stage 4)
        -> scoring_service.score_options                    (stage 5)
        -> Best purchasing options

This is the single entrypoint the get_recommendations MCP tool calls --
everything upstream (retrieval/purchase_options/scoring) is an internal
implementation detail, per the services/ vs mcp_server/ split.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from services.purchase_options_service import (
    PurchaseOption,
    apply_budget_filter,
    attach_commerce_data,
    build_purchase_options,
)
from services.retrieval_service import BuyerIntent, retrieve
from services.scoring_service import score_options


def _serialize(o: PurchaseOption) -> dict:
    return {
        "option_type": o.option_type.value,
        "items": [
            {"product_id": i.product_id, "sku": i.sku, "name": i.name, "qty": i.qty, "unit_price": i.unit_price}
            for i in o.items
        ],
        "list_price": round(o.list_price, 2),
        "total_price": round(o.total_price, 2),
        "savings": o.savings,
        "savings_percent": o.savings_percent,
        "budget_status": o.budget_status.value if o.budget_status else None,
        "score": round(o.score, 4),
        "reason": "; ".join(o.reasons) if o.reasons else "Good general match",
    }


def get_recommendations(
    session: Session,
    *,
    product_name: str | None = None,
    category: str | None = None,
    color: str | None = None,
    size: str | None = None,
    keywords: list[str] | None = None,
    budget: float | None = None,
    cart_id: str | None = None,
    limit: int = 5,
) -> list[dict]:
    intent = BuyerIntent(
        product_name=product_name,
        category=category,
        color=color,
        size=size,
        keywords=keywords or [],
    )

    candidates = retrieve(session, intent)
    commerce_candidates = attach_commerce_data(session, candidates)
    options = build_purchase_options(session, commerce_candidates)
    options = apply_budget_filter(options, budget)
    options = score_options(session, options, cart_id=cart_id)

    return [_serialize(o) for o in options[:limit]]