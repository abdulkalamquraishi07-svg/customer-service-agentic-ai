from decimal import Decimal

import pytest
from csagent_core.models import OrderStatus
from csagent_core.policy import Reason, ReturnPolicy, evaluate_eligibility
from support import T0, make_order


def test_delivered_in_window_is_eligible():
    d = evaluate_eligibility(make_order(delivered_days_ago=2), [], T0)
    assert d.eligible and d.refund_amount == Decimal("2499") and not d.requires_approval


def test_window_depends_on_category():
    # electronics: 10 days; accessories: 30 days
    assert not evaluate_eligibility(
        make_order(category="electronics", delivered_days_ago=11), [], T0
    ).eligible
    assert evaluate_eligibility(
        make_order(category="accessories", delivered_days_ago=11), [], T0
    ).eligible


def test_window_boundary_is_inclusive():
    assert evaluate_eligibility(
        make_order(category="electronics", delivered_days_ago=10), [], T0
    ).eligible


def test_expired_window_gives_reason_and_zero_refund():
    d = evaluate_eligibility(make_order(category="electronics", delivered_days_ago=30), [], T0)
    assert d.reasons == (Reason.RETURN_WINDOW_EXPIRED,)
    assert d.refund_amount == 0


@pytest.mark.parametrize("status", [OrderStatus.PROCESSING, OrderStatus.SHIPPED])
def test_undelivered_not_eligible(status):
    d = evaluate_eligibility(make_order(status=status, delivered_days_ago=None), [], T0)
    assert d.reasons == (Reason.ORDER_NOT_DELIVERED,)


def test_cancelled_not_eligible():
    d = evaluate_eligibility(make_order(status=OrderStatus.CANCELLED), [], T0)
    assert d.reasons == (Reason.ORDER_CANCELLED,)


def test_high_value_requires_approval():
    d = evaluate_eligibility(make_order(amount="74999", category="electronics"), [], T0)
    assert d.eligible and d.requires_approval


def test_approval_threshold_is_configurable_and_exclusive():
    policy = ReturnPolicy(approval_threshold=Decimal("2499"))
    assert not evaluate_eligibility(make_order(amount="2499"), [], T0, policy).requires_approval
    assert evaluate_eligibility(make_order(amount="2500"), [], T0, policy).requires_approval
