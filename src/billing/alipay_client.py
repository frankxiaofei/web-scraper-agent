"""支付宝 client stub（Commercial C2 C2-13）。"""

from __future__ import annotations

from src.core.config import get_settings


class AlipayNotImplementedError(NotImplementedError):
    pass


class AlipayClient:
    """国内支付预留接口；默认抛出 NotImplemented，可通过 ALIPAY_ENABLED 切换。"""

    def __init__(self) -> None:
        self._settings = get_settings()

    @property
    def enabled(self) -> bool:
        return bool(getattr(self._settings, "alipay_enabled", False))

    def create_order(
        self,
        *,
        tenant_id: str,
        plan_id: str,
        amount_cny: int,
        subject: str,
    ) -> dict:
        if not self.enabled:
            raise AlipayNotImplementedError("Alipay integration not enabled")
        return {
            "provider": "alipay",
            "tenant_id": tenant_id,
            "plan_id": plan_id,
            "amount_cny": amount_cny,
            "subject": subject,
            "order_string": "stub-order",
        }
