"""
One-time ingestion pipeline: raw seed CSV -> merchant database (docx section
3). After this runs, the database is the live source of truth; the CSV is
never touched again.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from db.models import Inventory, Product, Promotion
from db.session import get_session, init_db

RAW_CSV = Path(__file__).resolve().parent.parent / "data" / "raw" / "products.csv"
SAMPLE_PER_GROUP = 40  # rows kept per (name, variant) group -> ~480 SKUs total

NAME_TO_CATEGORY = {
    "Laptop": "Electronics",
    "Smartphone": "Electronics",
    "Monitor": "Electronics",
    "Headphones": "Electronics",
}

# Realistic-ish INR retail bands per product line, order-of-magnitude only.
PRICE_BANDS_INR = {
    "Laptop": (25_000, 95_000),
    "Smartphone": (8_000, 70_000),
    "Monitor": (6_000, 35_000),
    "Headphones": (1_200, 18_000),
}

OVERSTOCK_QUANTILE = 0.75  # top 25% stock within a name-group is "overstocked"


def _deterministic_discount(sku: str) -> float:
    """10-25%, stable per SKU rather than re-rolled every ingest run."""
    h = int(hashlib.sha1(sku.encode()).hexdigest(), 16)
    return round(10 + (h % 16), 1)  # 10.0 .. 25.0


def _describe(row: pd.Series) -> str:
    return (
        f"{row['Product Name']} ({row['color']}/{row['size']}). "
        f"{row['Warranty Period']}-year warranty. "
        f"Dimensions: {row['Product Dimensions']}. "
        f"Rated {row['Product Ratings']}/5 by previous buyers."
    )


def _derive_tags(row: pd.Series) -> str:
    price_tier = (
        "budget" if row["price_percentile"] < 0.33
        else "mid-range" if row["price_percentile"] < 0.66
        else "premium"
    )
    tags = [
        row["Product Name"].lower(),
        row["color"].lower(),
        row["size"].lower(),
        price_tier,
        f"{row['Warranty Period']}yr-warranty",
    ]
    if row["Product Ratings"] >= 4:
        tags.append("top-rated")
    return ",".join(tags)


def load_and_normalize(csv_path: Path = RAW_CSV) -> pd.DataFrame:
    df = pd.read_csv(csv_path)

    df["category"] = df["Product Name"].map(NAME_TO_CATEGORY)
    df[["color", "size"]] = df["Color/Size Variations"].str.split("/", expand=True)

    # rescale price per product line: keep each row's percentile position
    # within its own line's *full* raw distribution (computed pre-sample,
    # so the scale doesn't shift if SAMPLE_PER_GROUP changes), remap into
    # that line's realistic INR band.
    df["price_percentile"] = df.groupby("Product Name")["Price"].rank(pct=True)

    def _rescale(row: pd.Series) -> float:
        lo, hi = PRICE_BANDS_INR[row["Product Name"]]
        price = lo + row["price_percentile"] * (hi - lo)
        return round(price / 10) * 10  # round to nearest 10

    df["price_inr"] = df.apply(_rescale, axis=1)
    df["description"] = df.apply(_describe, axis=1)
    df["derived_tags"] = df.apply(_derive_tags, axis=1)

    # stratified sample: keep distribution, cut volume
    sampled_groups = [
        g.sample(n=min(SAMPLE_PER_GROUP, len(g)), random_state=42)
        for _, g in df.groupby(["Product Name", "Color/Size Variations"])
    ]
    df = pd.concat(sampled_groups, ignore_index=True)

    # overstock flag computed within each name-group, on the sampled set
    df["stock_quantile"] = df.groupby("Product Name")["Stock Quantity"].transform(
        lambda s: s.rank(pct=True)
    )
    df["is_overstocked"] = df["stock_quantile"] >= OVERSTOCK_QUANTILE

    return df


def ingest(csv_path: Path = RAW_CSV, drop_first: bool = True) -> dict:
    init_db(drop_first=drop_first)
    df = load_and_normalize(csv_path)
    session = get_session()

    now = datetime.now(timezone.utc)
    n_products = n_promotions = 0

    try:
        for _, row in df.iterrows():
            product = Product(
                sku=row["SKU"],
                source_row_id=row["Product ID"],
                name=row["Product Name"],
                category=row["category"],
                description=row["description"],
                price=float(row["price_inr"]),
                currency="INR",
                warranty_period_years=int(row["Warranty Period"]),
                dimensions_cm=row["Product Dimensions"],
                manufacturing_date=row["Manufacturing Date"],
                expiration_date=row["Expiration Date"],
                tags=row["derived_tags"],
                variant=row["Color/Size Variations"],
                color=row["color"],
                size=row["size"],
                rating=int(row["Product Ratings"]),
            )
            session.add(product)
            session.flush()  # assign product.id

            session.add(
                Inventory(
                    product_id=product.id,
                    stock_quantity=int(row["Stock Quantity"]),
                )
            )
            n_products += 1

            if row["is_overstocked"]:
                session.add(
                    Promotion(
                        product_id=product.id,
                        discount_percent=_deterministic_discount(row["SKU"]),
                        reason="Overstock clearance",
                        valid_from=now,
                        valid_until=now + timedelta(days=30),
                        is_active=True,
                    )
                )
                n_promotions += 1

        session.commit()
    finally:
        session.close()

    return {
        "products_ingested": n_products,
        "promotions_created": n_promotions,
        "source_rows": len(pd.read_csv(csv_path)),
    }


if __name__ == "__main__":
    stats = ingest()
    print(stats)