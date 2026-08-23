"""
Stage 1 of the recommendation pipeline: Buyer Intent -> candidates.

Deliberately keyword/attribute matching, not embeddings or free-text NLP --
at 480 SKUs across 4 product lines, exact + Jaccard-on-tags is enough, and
it stays fully explainable (every match has a traceable reason).

Buyer intent is STRUCTURED input (product_name / category / color / size /
free keywords), not a raw sentence -- the buyer agent is expected to have
already extracted these from whatever the human/agent actually said. This
module does not do intent parsing.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from db.models import Product


@dataclass
class BuyerIntent:
    product_name: str | None = None
    category: str | None = None
    color: str | None = None
    size: str | None = None
    keywords: list[str] = field(default_factory=list)

    def tokens(self) -> set[str]:
        parts = [self.product_name, self.category, self.color, self.size, *self.keywords]
        return {p.strip().lower() for p in parts if p}


@dataclass
class Candidate:
    product: Product
    match_type: str  # "exact" | "similar"
    match_score: float  # 0..1


def _product_tokens(p: Product) -> set[str]:
    tags = set(p.tags.split(",")) if p.tags else set()
    return tags | {p.name.lower(), p.category.lower(), p.color.lower(), p.size.lower()}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


EXACT_MATCH_OVERLAP = 0.75  # fraction of intent tokens that must be covered to count as "exact"
MAX_SIMILAR = 15


def retrieve(session: Session, intent: BuyerIntent) -> list[Candidate]:
    products = session.query(Product).filter(Product.is_active.is_(True)).all()
    intent_tokens = intent.tokens()

    # Browse mode: no usable intent -> everything is an equally-weighted
    # candidate, let stage 5's commerce signals (discount/value/urgency) do
    # the differentiating instead of relevance.
    if not intent_tokens:
        return [Candidate(product=p, match_type="similar", match_score=0.5) for p in products]

    exact: list[Candidate] = []
    similar: list[Candidate] = []

    for p in products:
        # explicit product_name intent is the strongest possible signal
        if intent.product_name and p.name.lower() == intent.product_name.strip().lower():
            exact.append(Candidate(product=p, match_type="exact", match_score=1.0))
            continue

        p_tokens = _product_tokens(p)
        covered = len(intent_tokens & p_tokens) / len(intent_tokens)
        if covered >= EXACT_MATCH_OVERLAP:
            exact.append(Candidate(product=p, match_type="exact", match_score=covered))
            continue

        score = _jaccard(intent_tokens, p_tokens)
        if score > 0:
            similar.append(Candidate(product=p, match_type="similar", match_score=score))

    similar.sort(key=lambda c: c.match_score, reverse=True)
    return exact + similar[:MAX_SIMILAR]