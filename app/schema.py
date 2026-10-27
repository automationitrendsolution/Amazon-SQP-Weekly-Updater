"""Field catalogue: SP-API Search Query Performance report JSON -> canonical record fields.

Each record is one (week, ASIN, search query) with snake_case canonical names. The
mapping to the columns of powerbidb.2024BrandAnalytics is made once the table has
been inspected (``run.py --inspect``).
"""
from __future__ import annotations

# (JSON section, JSON field, canonical name, kind)
# kind: "int" count, "dec" rate/share/score, "money" {amount, currencyCode}
METRIC_FIELDS: list[tuple[str, str, str, str]] = [
    ("searchQueryData", "searchQueryScore", "search_query_score", "int"),
    ("searchQueryData", "searchQueryVolume", "search_query_volume", "int"),
    ("impressionData", "totalQueryImpressionCount", "total_query_impression_count", "int"),
    ("impressionData", "asinImpressionCount", "asin_impression_count", "int"),
    ("impressionData", "asinImpressionShare", "asin_impression_share", "dec"),
    ("clickData", "totalClickCount", "total_click_count", "int"),
    ("clickData", "totalClickRate", "total_click_rate", "dec"),
    ("clickData", "asinClickCount", "asin_click_count", "int"),
    ("clickData", "asinClickShare", "asin_click_share", "dec"),
    ("clickData", "totalMedianClickPrice", "total_median_click_price", "money"),
    ("clickData", "asinMedianClickPrice", "asin_median_click_price", "money"),
    ("clickData", "totalSameDayShippingClickCount", "total_same_day_shipping_click_count", "int"),
    ("clickData", "totalOneDayShippingClickCount", "total_one_day_shipping_click_count", "int"),
    ("clickData", "totalTwoDayShippingClickCount", "total_two_day_shipping_click_count", "int"),
    ("cartAddData", "totalCartAddCount", "total_cart_add_count", "int"),
    ("cartAddData", "totalCartAddRate", "total_cart_add_rate", "dec"),
    ("cartAddData", "asinCartAddCount", "asin_cart_add_count", "int"),
    ("cartAddData", "asinCartAddShare", "asin_cart_add_share", "dec"),
    ("cartAddData", "totalMedianCartAddPrice", "total_median_cart_add_price", "money"),
    ("cartAddData", "asinMedianCartAddPrice", "asin_median_cart_add_price", "money"),
    ("cartAddData", "totalSameDayShippingCartAddCount", "total_same_day_shipping_cart_add_count", "int"),
    ("cartAddData", "totalOneDayShippingCartAddCount", "total_one_day_shipping_cart_add_count", "int"),
    ("cartAddData", "totalTwoDayShippingCartAddCount", "total_two_day_shipping_cart_add_count", "int"),
    ("purchaseData", "totalPurchaseCount", "total_purchase_count", "int"),
    ("purchaseData", "totalPurchaseRate", "total_purchase_rate", "dec"),
    ("purchaseData", "asinPurchaseCount", "asin_purchase_count", "int"),
    ("purchaseData", "asinPurchaseShare", "asin_purchase_share", "dec"),
    ("purchaseData", "totalMedianPurchasePrice", "total_median_purchase_price", "money"),
    ("purchaseData", "asinMedianPurchasePrice", "asin_median_purchase_price", "money"),
    ("purchaseData", "totalSameDayShippingPurchaseCount", "total_same_day_shipping_purchase_count", "int"),
    ("purchaseData", "totalOneDayShippingPurchaseCount", "total_one_day_shipping_purchase_count", "int"),
    ("purchaseData", "totalTwoDayShippingPurchaseCount", "total_two_day_shipping_purchase_count", "int"),
]

KEY_FIELDS = ["marketplace_id", "start_date", "end_date", "asin", "search_query"]


def canonical_fields() -> list[str]:
    names = KEY_FIELDS + ["search_query_normalized"]
    for _, _, name, kind in METRIC_FIELDS:
        names.append(name)
        if kind == "money":
            names.append(f"{name}_currency")
    return names


TABLE = "2024BrandAnalytics"

# Record field -> column of powerbidb.2024BrandAnalytics (inspected 2026-10-24), in table order.
# Not stored: the *_currency fields (the table is Amazon.com only; anything but USD is refused),
# marketplace_id and search_query_normalized. reporting_date / week / year / name / Index are
# filled by powerbi.py.
TABLE_COLUMNS: dict[str, str] = {
    "search_query": "search_query",
    "search_query_score": "search_query_score",
    "search_query_volume": "search_query_volume",
    "total_query_impression_count": "impressions_total_count",
    "asin_impression_count": "impressions_asin_count",
    "asin_impression_share": "impressions_asin_share_pct",
    "total_click_count": "clicks_total_count",
    "total_click_rate": "clicks_rate_pct",
    "asin_click_count": "clicks_asin_count",
    "asin_click_share": "clicks_asin_share_pct",
    "total_median_click_price": "clicks_price_median",
    "asin_median_click_price": "clicks_asin_price_median",
    "total_same_day_shipping_click_count": "clicks_shipping_same_day",
    "total_one_day_shipping_click_count": "clicks_shipping_1d",
    "total_two_day_shipping_click_count": "clicks_shipping_2d",
    "total_cart_add_count": "cart_adds_total_count",
    "total_cart_add_rate": "cart_adds_rate_pct",
    "asin_cart_add_count": "cart_adds_asin_count",
    "asin_cart_add_share": "cart_adds_asin_share_pct",
    "total_median_cart_add_price": "cart_adds_price_median",
    "asin_median_cart_add_price": "cart_adds_asin_price_median",
    "total_same_day_shipping_cart_add_count": "cart_adds_shipping_same_day",
    "total_one_day_shipping_cart_add_count": "cart_adds_shipping_1d",
    "total_two_day_shipping_cart_add_count": "cart_adds_shipping_2d",
    "total_purchase_count": "purchases_total_count",
    "total_purchase_rate": "purchases_rate_pct",
    "asin_purchase_count": "purchases_asin_count",
    "asin_purchase_share": "purchases_asin_share_pct",
    "total_median_purchase_price": "purchases_price_median",
    "asin_median_purchase_price": "purchases_asin_price_median",
    "total_same_day_shipping_purchase_count": "purchases_shipping_same_day",
    "total_one_day_shipping_purchase_count": "purchases_shipping_1d",
    "total_two_day_shipping_purchase_count": "purchases_shipping_2d",
}
METRIC_COLUMNS: tuple[str, ...] = tuple(c for f, c in TABLE_COLUMNS.items() if f != "search_query")
# The table keeps shares and rates as percentages and prices with 2 decimals (0.08 = 0.08 %),
# as Seller Central's SQP download shows them.
PERCENT_FIELDS = frozenset(name for _, _, name, kind in METRIC_FIELDS if kind == "dec")
PRICE_FIELDS = frozenset(name for _, _, name, kind in METRIC_FIELDS if kind == "money")


def quote(name: str) -> str:
    """Backtick-quote a MySQL identifier taken from the settings or a live schema."""
    if "`" in name or "\x00" in name:
        raise ValueError(f"Unsafe identifier {name!r}")
    return f"`{name}`"
