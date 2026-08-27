from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from db.models import Inventory, Product, Promotion
from db.session import get_session, init_db
from services.domain_vocab import BRANDS, COLORS, FEATURE_PHRASES

# NOTE: this must match the actual filename in data/raw/. The repo currently
# ships data/raw/amz_uk_processed_data_recovered.csv -- update this if you
# swap in a different export.
RAW_CSV = Path(__file__).resolve().parent.parent / "data" / "raw" / "amz_uk_processed_data_recovered.csv"
CHUNK_SIZE = 200_000
GBP_TO_INR = 105.0

PROMOTION_RATE = 0.25  # fraction of catalog seeded with a clearance promo (see _deterministic_is_promoted)

_COLOR_PATTERNS = sorted(COLORS, key=len, reverse=True)
_BRAND_RE = re.compile(r"\b(" + "|".join(re.escape(b) for b in BRANDS) + r")\b", re.IGNORECASE)


def _extract_brand(title: str) -> str:
    m = _BRAND_RE.search(title or "")
    return m.group(1).title() if m else "Unbranded"


def _extract_color(title: str) -> str:
    t = (title or "").lower()
    for color in _COLOR_PATTERNS:
        if color in t:
            return color.title()
    return "Standard"


def _extract_features(title: str) -> list[str]:
    t = (title or "").lower()
    found = []
    for phrase, tag in FEATURE_PHRASES.items():
        if phrase in t and tag not in found:
            found.append(tag)
    return found


def _deterministic_stock(asin: str) -> int:
    h = int(hashlib.sha1(asin.encode()).hexdigest(), 16)
    return 3 + (h % 148)  # 3 .. 150


def _deterministic_discount(asin: str) -> float:
    h = int(hashlib.sha1((asin + "promo").encode()).hexdigest(), 16)
    return round(10 + (h % 16), 1)  # 10.0 .. 25.0


def _deterministic_is_promoted(asin: str) -> bool:
    """~PROMOTION_RATE of SKUs get a seeded promotion, chosen deterministically
    from the SKU itself -- NOT from boughtInLastMonth or any other purchase-
    history figure. This is synthetic "the merchant is currently running a
    clearance on these items" data, independent of anyone's buying behavior."""
    h = int(hashlib.sha1((asin + "promo-eligible").encode()).hexdigest(), 16)
    bucket = max(1, round(1 / PROMOTION_RATE))
    return (h % bucket) == 0


def load_filtered(csv_path: Path = RAW_CSV) -> pd.DataFrame:
    kept_frames: list[pd.DataFrame] = []

    for chunk in pd.read_csv(
        csv_path,
        usecols=["asin", "title", "stars", "reviews", "price", "isBestSeller", "categoryName"],
        chunksize=CHUNK_SIZE,
    ):
        chunk = chunk[(chunk["price"] > 0) & (chunk["stars"] > 0)]
        chunk = chunk.dropna(subset=["categoryName", "title"])
        if len(chunk):
            kept_frames.append(chunk)

    if not kept_frames:
        raise ValueError("No rows survived the defensive filter -- check the CSV's price/stars columns.")

    df = pd.concat(kept_frames, ignore_index=True)
    df = df.drop_duplicates(subset="asin")

    # No per-category sampling cap: ingest every row that survives the
    # price/stars/dedup filters above.

    df["brand"] = df["title"].map(_extract_brand)
    df["color"] = df["title"].map(_extract_color)
    df["features"] = df["title"].map(_extract_features)
    df["price_inr"] = (df["price"] * GBP_TO_INR / 10).round() * 10

    # Which SKUs are "currently on promotion" is chosen from the SKU itself,
    # not from boughtInLastMonth or any other purchase-count signal -- see
    # _deterministic_is_promoted().
    df["is_promoted"] = df["asin"].map(_deterministic_is_promoted)

    return df


def _derive_tags(row: pd.Series) -> str:
    price_tier = (
        "budget" if row["price_percentile"] < 0.33
        else "mid-range" if row["price_percentile"] < 0.66
        else "premium"
    )
    tags = [
        row["categoryName"].lower().replace(" ", "-").replace(",", ""),
        row["brand"].lower(),
        row["color"].lower(),
        price_tier,
        *row["features"],
    ]
    if row["isBestSeller"]:
        tags.append("bestseller")
    if row["stars"] >= 4.3:
        tags.append("top-rated")
    return ",".join(tags)


def ingest(csv_path: Path = RAW_CSV, drop_first: bool = True, batch_size: int = 5000) -> dict:
    """Batches inserts (flush per batch to resolve Product.id -> children,
    commit per batch) instead of flushing every single row -- at ~100K+ rows
    a flush-per-row pattern is unusably slow."""
    init_db(drop_first=drop_first)
    df = load_filtered(csv_path)
    df["price_percentile"] = df.groupby("categoryName")["price_inr"].rank(pct=True)

    session = get_session()
    now = datetime.now(timezone.utc)
    n_products = n_promotions = 0

    try:
        batch: list[tuple[Product, pd.Series]] = []

        def flush_batch() -> None:
            nonlocal n_products, n_promotions
            if not batch:
                return
            session.add_all([p for p, _ in batch])
            session.flush()  # assigns .id to every Product object in this batch

            children = []
            for product, row in batch:
                children.append(Inventory(product_id=product.id, stock_quantity=_deterministic_stock(str(row["asin"]))))
                if row["is_promoted"]:
                    children.append(Promotion(
                        product_id=product.id,
                        discount_percent=_deterministic_discount(str(row["asin"])),
                        reason="Merchant clearance promotion",
                        valid_from=now,
                        valid_until=now + timedelta(days=30),
                        is_active=True,
                    ))
                    n_promotions += 1
                n_products += 1
            session.add_all(children)
            session.commit()
            batch.clear()

        for _, row in df.iterrows():
            title = str(row["title"])[:300]
            product = Product(
                sku=str(row["asin"]),
                source_row_id=str(row["asin"]),
                # FIX: name/description were swapped in the original script --
                # `name` was being set to the category ("Electronics") and the
                # real product title only lived in `description`. name is now
                # the (truncated) title, category stays the category.
                name=title[:200],
                category=str(row["categoryName"]),
                description=title,
                price=float(row["price_inr"]),
                currency="INR",
                warranty_period_years=1,
                dimensions_cm="N/A",
                manufacturing_date="N/A",
                expiration_date="N/A",
                tags=_derive_tags(row),
                brand=str(row["brand"]),
                variant=f"{row['color']}/Standard",
                color=row["color"],
                size="Standard",
                rating=max(1, min(5, round(row["stars"]))),
            )
            batch.append((product, row))
            if len(batch) >= batch_size:
                flush_batch()

        flush_batch()
    finally:
        session.close()

    return {
        "products_ingested": n_products,
        "promotions_created": n_promotions,
        "categories": sorted(df["categoryName"].unique().tolist()),
    }


if __name__ == "__main__":
    print(ingest())