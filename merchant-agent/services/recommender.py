"""
Turns a buyer's free-text intent (e.g. "phone under 3000 which is blue and
samsung with good camera quality") into a structured, ranked list of offers --
list price, active discount, effective price, stock, and a suggested
cross-sell bundle -- ready to hand to a buyer agent as JSON.

This is intentionally rule-based (regex + vocab lookups), not an LLM or an ML
ranker: for a hackathon dataset of ~1,200 SKUs it's fast, deterministic, and
easy to explain in an audit trail -- which matters more here than recall.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select

from db.models import Product
from db.session import get_session
from services.domain_vocab import BRANDS, COLORS, FEATURE_PHRASES

_BRAND_RE = re.compile(r"\b(" + "|".join(re.escape(b) for b in BRANDS) + r")\b", re.IGNORECASE)
_COLOR_PATTERNS = sorted(COLORS, key=len, reverse=True)

_PRICE_PATTERNS = [
    re.compile(r"under\s*(?:inr|rs\.?|₹)?\s*([\d,]+)", re.IGNORECASE),
    re.compile(r"below\s*(?:inr|rs\.?|₹)?\s*([\d,]+)", re.IGNORECASE),
    re.compile(r"(?:less than|<=|<)\s*(?:inr|rs\.?|₹)?\s*([\d,]+)", re.IGNORECASE),
    re.compile(r"budget\s*(?:of|is)?\s*(?:inr|rs\.?|₹)?\s*([\d,]+)", re.IGNORECASE),
]

_QUALITY_WORDS = ("good", "great", "best", "excellent", "high quality", "top")
_STOPWORDS = {
    "i", "want", "to", "buy", "a", "an", "the", "which", "is", "and", "has",
    "have", "with", "for", "of", "in", "that", "under", "below", "budget",
    "some", "any", "me", "looking", "need", "quality",
}


@dataclass
class ParsedIntent:
    raw_text: str
    max_price: float | None = None
    min_rating: int | None = None
    brand: str | None = None
    color: str | None = None
    features: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "raw_text": self.raw_text,
            "max_price": self.max_price,
            "min_rating": self.min_rating,
            "brand": self.brand,
            "color": self.color,
            "features": self.features,
            "keywords": self.keywords,
        }


def parse_intent(text: str) -> ParsedIntent:
    t = text.strip()
    lower = t.lower()

    max_price = None
    for rx in _PRICE_PATTERNS:
        m = rx.search(lower)
        if m:
            max_price = float(m.group(1).replace(",", ""))
            break

    brand = None
    bm = _BRAND_RE.search(t)
    if bm:
        brand = bm.group(1).title()

    color = None
    for c in _COLOR_PATTERNS:
        if c in lower:
            color = c.title()
            break

    features: list[str] = []
    for phrase, tag in FEATURE_PHRASES.items():
        if phrase in lower and tag not in features:
            features.append(tag)

    min_rating = 4 if any(w in lower for w in _QUALITY_WORDS) else None
    if re.search(r"\b5\s*-?\s*star", lower):
        min_rating = 5

    tokens = re.findall(r"[a-z0-9\-]+", lower)
    structural = {w.lower() for w in (brand, color) if w}
    for f in features:
        structural.add(f)
    keywords = [tok for tok in tokens if tok not in _STOPWORDS and tok not in structural and len(tok) > 2 and not tok.isdigit()]

    return ParsedIntent(
        raw_text=t, max_price=max_price, min_rating=min_rating,
        brand=brand, color=color, features=features, keywords=keywords,
    )


def _best_active_promo(product: Product, at: datetime | None = None):
    at = at or datetime.now(timezone.utc)
    best = None
    for promo in product.promotions:
        if promo.is_currently_valid(at):
            if best is None or promo.discount_percent > best.discount_percent:
                best = promo
    return best


def _matches_keywords(product: Product, keywords: list[str]) -> bool:
    if not keywords:
        return True
    haystack = " ".join(filter(None, [product.name, product.category, product.description, product.tags])).lower()
    return any(k in haystack for k in keywords)


def _match_score(p: Product, intent: "ParsedIntent", discount_pct: float) -> float:
    """Attribute-match strength against what the buyer actually asked for,
    plus rating and bestseller as quality/curation signals (both are
    product-level attributes set by the merchant/catalog, not a record of
    this-user's or any user's purchase behavior). Explicitly NOT used:
    boughtInLastMonth or any other raw "how many times was this bought"
    figure -- that stays out of ranking entirely."""
    score = 0.0
    if intent.brand and (p.brand or "").lower() == intent.brand.lower():
        score += 3
    if intent.color and (p.color or "").lower() == intent.color.lower():
        score += 3
    tagset = set(p.tag_list())
    score += 2 * sum(1 for f in intent.features if f in tagset)
    haystack = " ".join(filter(None, [p.name, p.category, p.description, p.tags])).lower()
    score += 1 * sum(1 for k in intent.keywords if k in haystack)
    score += discount_pct / 100.0        # small tiebreaker: current active promo, not history
    score += (p.rating or 0) * 0.5       # quality signal, capped contribution (max +2.5)
    if "bestseller" in tagset:
        score += 1.5                     # merchant-curated badge, not a raw purchase count
    return score


def _similarity_score(p: Product, intent: "ParsedIntent") -> float:
    """Relaxed content-based similarity against the buyer's intent profile --
    used for the 'similar products' fallback when nothing satisfies every
    hard filter. Same ingredients as _match_score: attribute overlap, price
    closeness to budget, plus rating/bestseller as tie-break quality signals.
    Still nothing from boughtInLastMonth or any raw purchase-count data."""
    score = 0.0
    if intent.brand and (p.brand or "").lower() == intent.brand.lower():
        score += 2
    if intent.color and (p.color or "").lower() == intent.color.lower():
        score += 2
    tagset = set(p.tag_list())
    score += 1.5 * sum(1 for f in intent.features if f in tagset)
    haystack = " ".join(filter(None, [p.name, p.category, p.description, p.tags])).lower()
    score += 1 * sum(1 for k in intent.keywords if k in haystack)
    if intent.max_price:
        # closer to (or under) budget scores higher; falls off past 1.5x budget
        ratio = p.price / intent.max_price
        score += max(0.0, 1.5 - ratio)
    score += (p.rating or 0) * 0.3
    if "bestseller" in tagset:
        score += 1.0
    return score


def find_similar_products(intent: "ParsedIntent", exclude_skus: set[str], limit: int = 5) -> list[dict]:
    """Content-based 'similar products' recommendations: ranks in-stock,
    active products by similarity to the buyer's stated intent, with no
    popularity/rating/historic-sales signal involved."""
    session = get_session()
    try:
        stmt = select(Product).where(Product.is_active.is_(True))
        candidates = session.execute(stmt).scalars().all()

        scored = []
        for p in candidates:
            if p.sku in exclude_skus:
                continue
            inv = p.inventory
            if not inv or inv.available_quantity <= 0:
                continue
            sim = _similarity_score(p, intent)
            if sim <= 0:
                continue
            promo = _best_active_promo(p)
            discount_pct = promo.discount_percent if promo else 0.0
            effective_price = round(p.price * (1 - discount_pct / 100), 2)
            scored.append({
                "sku": p.sku,
                "name": p.name,
                "brand": p.brand,
                "category": p.category,
                "color": p.color,
                "rating": p.rating,
                "list_price": p.price,
                "currency": p.currency,
                "discount_percent": discount_pct,
                "effective_price": effective_price,
                "stock_available": inv.available_quantity,
                "tags": p.tag_list(),
                "_similarity": round(sim, 2),
            })

        scored.sort(key=lambda o: o["_similarity"], reverse=True)
        top = scored[:limit]
        for o in top:
            o.pop("_similarity", None)
        return top
    finally:
        session.close()


def _suggest_bundle(top_offers: list[dict], all_offers: list[dict]) -> dict | None:
    """Heuristic cross-sell: pair the top offer with the cheapest in-budget
    item from the same category. Not backed by a stored Promotion -- this is
    a suggestion for the agent to propose, and should be shown as such."""
    if not top_offers:
        return None
    primary = top_offers[0]
    same_category = [
        o for o in all_offers
        if o["category"] == primary["category"] and o["sku"] != primary["sku"]
    ]
    if not same_category:
        return None
    companion = min(same_category, key=lambda o: o["effective_price"])

    combined_list = primary["list_price"] + companion["list_price"]
    combined_effective = primary["effective_price"] + companion["effective_price"]
    bundle_discount_percent = 5.0  # suggested extra incentive on top of existing promos
    bundle_price = round(combined_effective * (1 - bundle_discount_percent / 100), 2)

    return {
        "type": "cross-sell bundle suggestion (not a stored Promotion row)",
        "items": [primary["sku"], companion["sku"]],
        "combined_list_price": round(combined_list, 2),
        "combined_effective_price": round(combined_effective, 2),
        "suggested_bundle_discount_percent": bundle_discount_percent,
        "suggested_bundle_price": bundle_price,
        "estimated_extra_savings": round(combined_effective - bundle_price, 2),
        "note": "Heuristic upsell for the agent to propose to the buyer.",
    }


def recommend(query_text: str, limit: int = 5) -> dict:
    """Main entrypoint: free-text buyer intent -> structured offers dict."""
    intent = parse_intent(query_text)
    session = get_session()
    try:
        stmt = select(Product).where(Product.is_active.is_(True))
        if intent.brand:
            stmt = stmt.where(Product.brand.ilike(intent.brand))
        if intent.color:
            stmt = stmt.where(Product.color.ilike(intent.color))
        candidates = session.execute(stmt).scalars().all()

        offers: list[dict] = []
        for p in candidates:
            inv = p.inventory
            if not inv or inv.available_quantity <= 0:
                continue
            if intent.min_rating and (p.rating or 0) < intent.min_rating:
                continue
            if intent.features:
                tagset = set(p.tag_list())
                if not any(f in tagset for f in intent.features):
                    continue
            if not _matches_keywords(p, intent.keywords):
                continue

            promo = _best_active_promo(p)
            discount_pct = promo.discount_percent if promo else 0.0
            effective_price = round(p.price * (1 - discount_pct / 100), 2)

            if intent.max_price is not None and effective_price > intent.max_price:
                continue

            score = _match_score(p, intent, discount_pct)

            offers.append({
                "sku": p.sku,
                "name": p.name,
                "brand": p.brand,
                "category": p.category,
                "color": p.color,
                "rating": p.rating,
                "list_price": p.price,
                "currency": p.currency,
                "discount_percent": discount_pct,
                "effective_price": effective_price,
                "savings": round(p.price - effective_price, 2),
                "promotion_reason": promo.reason if promo else None,
                "promotion_valid_until": promo.valid_until.isoformat() if promo else None,
                "stock_available": inv.available_quantity,
                "tags": p.tag_list(),
                "_score": score,
            })

        offers.sort(key=lambda o: o["_score"], reverse=True)
        top = [dict(o) for o in offers[:limit]]
        for o in top:
            o.pop("_score", None)
        for o in offers:
            o.pop("_score", None)

        bundle = _suggest_bundle(top, offers)

        # Always surface similar products too (not just as a zero-result
        # fallback) -- "matching AND similar products", per the brief.
        exclude = {o["sku"] for o in top}
        similar = find_similar_products(intent, exclude_skus=exclude, limit=limit)

        return {
            "query": query_text,
            "parsed_intent": intent.as_dict(),
            "matching_offer_count": len(offers),
            "offers": top,
            "bundle_suggestion": bundle,
            "similar_products": similar,
        }
    finally:
        session.close()