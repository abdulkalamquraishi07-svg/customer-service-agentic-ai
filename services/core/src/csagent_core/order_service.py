"""Order lookups. Read-only; every query is scoped to the caller's customer_id."""

from csagent_core.context import CallerContext
from csagent_core.errors import NotFound, ValidationFailed
from csagent_core.models import Order, OrderStatus, ShipmentView
from csagent_core.ports import Clock, OrderRepository

_TERMINAL = {OrderStatus.DELIVERED, OrderStatus.CANCELLED}


class OrderService:
    def __init__(self, orders: OrderRepository, clock: Clock) -> None:
        self._orders = orders
        self._clock = clock

    def _view(self, order: Order) -> ShipmentView:
        now = self._clock()
        overdue = order.status not in _TERMINAL and now > order.promised_delivery_at
        return ShipmentView(
            order_id=order.order_id,
            status=order.status,
            shipment_status=order.shipment_status,
            carrier=order.carrier,
            tracking_number=order.tracking_number,
            last_scan=order.last_scan,
            ordered_at=order.ordered_at,
            promised_delivery_at=order.promised_delivery_at,
            delivered_at=order.delivered_at,
            is_overdue=overdue,
            days_overdue=(now - order.promised_delivery_at).days if overdue else 0,
            category=order.category,
            item_names=[item.name for item in order.items],
        )

    def get_order(self, ctx: CallerContext, order_id: str) -> Order:
        if not order_id or len(order_id) > 64:
            raise ValidationFailed("invalid order_id")
        # Customer id comes from the caller context only. A foreign order id is
        # indistinguishable from a missing one.
        order = self._orders.get(ctx.customer_id, order_id)
        if order is None:
            raise NotFound("order not found")
        return order

    def get_shipment_status(self, ctx: CallerContext, order_id: str) -> ShipmentView:
        return self._view(self.get_order(ctx, order_id))

    def list_orders(self, ctx: CallerContext) -> list[ShipmentView]:
        orders = sorted(
            self._orders.list_for_customer(ctx.customer_id),
            key=lambda o: o.ordered_at,
            reverse=True,
        )
        return [self._view(o) for o in orders]
