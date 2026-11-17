
import os

import httpx


MOCK_ERP_BASE_URL = os.getenv(
    "MOCK_ERP_BASE_URL",
    "http://127.0.0.1:8001"
)


class MockERPClient:
    """HTTP client for the Mock ERP REST API."""

    def __init__(self):
        self.base_url = MOCK_ERP_BASE_URL

    def get_purchase_orders(
        self,
        limit: int = 20,
        offset: int = 0
    ):
        url = f"{self.base_url}/api/v1/purchase-orders"

        with httpx.Client(timeout=10.0) as client:
            response = client.get(
                url,
                params={
                    "limit": limit,
                    "offset": offset
                }
            )

            response.raise_for_status()

            return response.json()
