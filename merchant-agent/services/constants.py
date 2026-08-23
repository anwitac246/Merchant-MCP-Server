"""Tunable constants for the recommendation pipeline, kept in one place so
retrieval/purchase-options/scoring never disagree with each other."""

# Stage 4 — budget filter
STRETCH_PCT = 0.05  # up to 5% over budget is a "stretch" option, not rejected

# Stage 2/3 — bundles and group offers
BUNDLE_DISCOUNT_PCT = 5.0  # product + companion-category product
GROUP_TIERS: list[tuple[int, float]] = [  # (min_qty, discount_percent), highest qty first
    (4, 10.0),
    (2, 5.0),
]

# Stage 2 — inventory
LOW_STOCK_THRESHOLD = 10

# Stage 5 — scoring normalization
# Highest discount any single option can carry: a promo (<=25%, see ingestion)
# stacked conceptually against bundle/group ceilings -- used only to keep
# discount_score in 0..1, not to actually stack discounts.
MAX_DISCOUNT_PERCENT = 25.0

# Static companion map -- which product lines pair with which, for both
# BUNDLE construction and cart-based cross-sell scoring. Hand-written
# because the catalog only has 4 product lines; swap for a co-purchase
# derived table if/when real order history exists.
COMPANION_CATEGORIES: dict[str, set[str]] = {
    "Laptop": {"Headphones", "Monitor"},
    "Smartphone": {"Headphones"},
    "Monitor": {"Laptop"},
    "Headphones": {"Laptop", "Smartphone"},
}

# Stage 5 — final weighted score. Must sum to 1.0.
WEIGHTS = {
    "match": 0.25,
    "budget_fit": 0.20,
    "discount": 0.20,
    "value": 0.15,
    "cross_sell": 0.10,
    "urgency": 0.10,
}
assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9