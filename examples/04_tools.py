"""Give the model your own functions.

Tools are ordinary Python callables. They run in *your* process (not in the
sandbox), so they can use any library, open connections, or read credentials
the model's code never sees. The model is shown the signature and docstring.

    python examples/04_tools.py
"""

import rlm_ig as rlm

PRICES = {"SKU-104": 19.99, "SKU-221": 4.50, "SKU-309": 129.00, "SKU-412": 0.99}
STOCK = {"SKU-104": 12, "SKU-221": 0, "SKU-309": 3, "SKU-412": 840}


def price_of(sku: str) -> float:
    """Current unit price of a SKU in USD. Raises KeyError for an unknown SKU."""
    return PRICES[sku]


def units_in_stock(sku: str) -> int:
    """How many units of a SKU are in the warehouse right now."""
    return STOCK[sku]


order = "Order #8812: 3 x SKU-104, 10 x SKU-221, 1 x SKU-309, 200 x SKU-412"

result = rlm.run(
    "For this order, work out the total price and list any line that cannot be fulfilled from stock.",
    order,
    tools=[price_of, units_in_stock],
    schema={
        "type": "object",
        "properties": {
            "total_usd": {"type": "number"},
            "unfulfillable": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["total_usd", "unfulfillable"],
    },
)
print(result.answer)
