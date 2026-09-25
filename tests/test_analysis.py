from __future__ import annotations

from student_agent.analysis import analyze_items, analyze_payment, analyze_shipment, at


def test_payment_ignores_capture_and_refund_from_other_purchase_episode() -> None:
    first = at("2018-01-07T09:00:00-03:00")
    second = at("2018-04-23T09:00:00-03:00")
    assert first is not None and second is not None
    result = analyze_payment(
        [{"payment_value": "35.00"}, {"payment_value": "89.00"}],
        {
            "events": [
                {
                    "event_at": "2018-01-07T10:00:00-03:00",
                    "event_type": "captured",
                    "status": "confirmed",
                    "amount_brl": "35.00",
                },
                {
                    "event_at": "2018-01-07T12:00:00-03:00",
                    "event_type": "reconciliation_mismatch",
                    "status": "open",
                    "amount_brl": "35.00",
                },
                {
                    "event_at": "2018-04-23T10:00:00-03:00",
                    "event_type": "captured",
                    "status": "confirmed",
                    "amount_brl": "89.00",
                },
            ]
        },
        {
            "events": [
                {
                    "event_at": "2018-05-04T09:00:00-03:00",
                    "status": "pending",
                    "amount_brl": "89.00",
                }
            ]
        },
        first,
        [first, second],
        89.0,
    )
    assert result["verdict"] == "capture_mismatch"
    assert result["captured"] == 35.0
    assert result["refund_events"] == []


def test_seller_event_overrides_generic_late_delivery() -> None:
    first = at("2018-07-04T09:00:00-03:00")
    second = at("2018-07-27T09:00:00-03:00")
    assert first is not None and second is not None
    result = analyze_shipment(
        {
            "delivered_carrier_at": "2018-07-11T09:00:00-03:00",
            "delivered_customer_at": "2018-07-18T09:00:00-03:00",
            "estimated_delivery_at": "2018-07-14T09:00:00-03:00",
            "shipping_limits": [
                {"seller_id": "seller-a", "shipping_limit_at": "2018-07-07T09:00:00-03:00"},
                {"seller_id": "seller-b", "shipping_limit_at": "2018-07-30T09:00:00-03:00"},
            ],
            "events": [
                {
                    "event_at": "2018-07-18T09:00:00-03:00",
                    "event_type": "delivered_late",
                    "actor": "seller",
                    "status": "confirmed",
                }
            ],
        },
        [],
        first,
        [first, second],
    )
    assert result["verdict"] == "seller_delay"
    assert result["late_seller_ids"] == ["seller-a"]


def test_item_total_excludes_older_duplicate_order_row() -> None:
    first = at("2017-12-20T09:00:00-03:00")
    second = at("2018-05-11T09:00:00-03:00")
    assert first is not None and second is not None
    result = analyze_items(
        [
            {
                "order_item_id": "item-1",
                "seller_id": "seller-1",
                "shipping_limit_date": "2018-05-14T09:00:00-03:00",
                "price": "79.00",
                "freight_value": "10.00",
            },
            {
                "order_item_id": "item-1",
                "seller_id": "seller-1",
                "shipping_limit_date": "2017-12-23T09:00:00-03:00",
                "price": "79.00",
                "freight_value": "18.00",
            },
        ],
        second,
        [first, second],
    )
    assert result["total"] == 89.0
    assert result["item_ids"] == ["item-1"]
