"""Kalshi API client with RSA-PSS signature authentication."""
import base64
import hashlib
import logging
import time
from pathlib import Path
from typing import Any, Dict, Optional

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from backend.config import settings

logger = logging.getLogger("trading_bot")

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"

# Kalshi v2 order endpoints (POST/DELETE moved from /portfolio/orders to
# /portfolio/events/orders in the v2 API migration; GET still at old path).
_ORDER_WRITE_PATH = "/portfolio/events/orders"   # POST create, DELETE cancel
_ORDER_READ_PATH  = "/portfolio/orders"           # GET single order (old path still works)


class KalshiClient:
    """Async Kalshi API client using RSA-PSS signature auth."""

    def __init__(self):
        self._private_key = None

    def _load_private_key(self):
        """Load RSA private key from file (lazy, cached)."""
        if self._private_key is not None:
            return self._private_key

        key_path = settings.KALSHI_PRIVATE_KEY_PATH
        if not key_path:
            raise ValueError("KALSHI_PRIVATE_KEY_PATH not configured")

        pem_data = Path(key_path).expanduser().read_bytes()
        self._private_key = serialization.load_pem_private_key(pem_data, password=None)
        return self._private_key

    def _sign_request(self, method: str, path: str) -> Dict[str, str]:
        """
        Generate auth headers for a Kalshi API request.

        Signature = RSA-PSS-sign(timestamp_ms + METHOD + path)
        where path = /trade-api/v2/... (no query params).
        """
        timestamp_ms = str(int(time.time() * 1000))
        message = f"{timestamp_ms}{method.upper()}{path}"

        private_key = self._load_private_key()
        signature = private_key.sign(
            message.encode("utf-8"),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.MAX_LENGTH,
            ),
            hashes.SHA256(),
        )

        return {
            "KALSHI-ACCESS-KEY": settings.KALSHI_API_KEY_ID,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode("utf-8"),
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
            "Content-Type": "application/json",
        }

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        body: Optional[dict] = None,
    ) -> dict:
        """Authenticated request helper (GET, POST, DELETE)."""
        full_path = f"/trade-api/v2{path}"
        url = f"{BASE_URL}{path}"
        headers = self._sign_request(method, full_path)
        async with httpx.AsyncClient(timeout=15.0) as client:
            if method == "GET":
                response = await client.get(url, headers=headers, params=params)
            elif method == "POST":
                response = await client.post(url, headers=headers, json=body)
            elif method == "DELETE":
                response = await client.delete(url, headers=headers)
            else:
                raise ValueError(f"Unsupported HTTP method: {method}")
            response.raise_for_status()
            return response.json()

    async def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> dict:
        """Authenticated GET request to Kalshi API."""
        return await self._request("GET", path, params=params)

    async def get_markets(self, params: Optional[Dict[str, Any]] = None) -> dict:
        """Fetch markets with optional filters."""
        return await self.get("/markets", params=params)

    async def get_market(self, ticker: str) -> dict:
        """Fetch a single market by ticker."""
        return await self.get(f"/markets/{ticker}")

    async def get_orderbook(self, ticker: str) -> dict:
        """
        Fetch live order book depth for a single market.

        Returns the full-precision (dollar-denominated) order book:
            {
              "orderbook_fp": {
                "yes_dollars": [[price_float, size], ...],  # YES bids, best first
                "no_dollars":  [[price_float, size], ...]   # NO bids, best first
              }
            }

        Each entry is a [price, size] pair where price is a float in [0, 1].
        Entries represent bids (buyers). To derive ask prices:
            yes_ask = 1.0 - max(no_dollars  prices)   # best YES offer
            no_ask  = 1.0 - max(yes_dollars prices)   # best NO offer
        """
        return await self.get(f"/markets/{ticker}/orderbook")

    async def get_balance(self) -> dict:
        """Get portfolio balance (useful for auth test)."""
        return await self.get("/portfolio/balance")

    async def place_order(
        self,
        ticker: str,
        side: str,
        count: int,
        limit_price_cents: int,
        post_only: bool = True,
    ) -> dict:
        """
        Place a resting maker limit buy order via the Kalshi v2 API.

        Args:
            ticker:             Market ticker (e.g. "KXHIGHNY-26AUG10-B85.0")
            side:               "yes" — buy YES contracts;
                                "no"  — buy NO contracts (placed as a YES ask)
            count:              Number of contracts to buy
            limit_price_cents:  Limit price for the CONTRACT being bought, in
                                integer cents (1–99).  For YES orders this is the
                                YES bid; for NO orders this is the NO bid (we
                                derive the equivalent YES ask internally).
            post_only:          If True the order is cancelled rather than
                                crossing the spread (maker-only).

        Returns a dict normalised to {"order": {"order_id": "...", ...}} so
        callers written against the old response shape keep working.

        v2 API notes (discovered 2026-08-12):
          - Endpoint moved: POST /portfolio/orders → POST /portfolio/events/orders
          - "side" field: "bid" (buy YES) or "ask" (sell YES = buy NO)
          - "price" field: dollar decimal string, e.g. "0.0500" (not integer cents)
          - "count" field: string, not integer
          - New required fields: time_in_force, self_trade_prevention_type
          - Removed fields: action, order_type, yes_price / no_price
        """
        if side == "yes":
            api_side = "bid"
            price_str = f"{limit_price_cents / 100:.4f}"
        else:
            # Buying NO at limit_price_cents means selling YES at the complement
            api_side = "ask"
            yes_price_cents = 100 - limit_price_cents
            price_str = f"{yes_price_cents / 100:.4f}"

        body = {
            "ticker":                       ticker,
            "side":                         api_side,
            "count":                        str(count),
            "price":                        price_str,
            "time_in_force":                "good_till_canceled",
            "self_trade_prevention_type":   "maker",
            "post_only":                    post_only,
        }
        raw = await self._request("POST", _ORDER_WRITE_PATH, body=body)
        # Normalise: new API returns order_id at top level; wrap for callers
        # that expect {"order": {"order_id": "..."}}
        if "order_id" in raw and "order" not in raw:
            return {"order": raw}
        return raw

    async def cancel_order(self, order_uuid: str) -> dict:
        """Cancel a resting limit order by UUID (v2 endpoint)."""
        return await self._request("DELETE", f"{_ORDER_WRITE_PATH}/{order_uuid}")

    async def get_order(self, order_uuid: str) -> dict:
        """Get current status of a limit order (GET still served at old path)."""
        return await self.get(f"{_ORDER_READ_PATH}/{order_uuid}")

    async def get_positions(self, settlement_status: str = "unsettled") -> dict:
        """
        Fetch all portfolio positions from Kalshi.

        Args:
            settlement_status: "unsettled" (default) returns only open positions.
                               Pass "" or None to return all positions.

        Response shape (v2):
            {"positions": [{"ticker": "...", "position": 5, "market_exposure": "5.00",
                            "unrealized_pnl": "1.25", "total_traded": "3", ...}, ...]}

        The "position" field is a signed integer:
            positive → net long YES contracts
            negative → net long NO contracts
            zero     → flat (no open position)
        """
        params: Dict[str, Any] = {}
        if settlement_status:
            params["settlement_status"] = settlement_status
        return await self.get("/portfolio/positions", params=params)

    async def sell_position(
        self,
        ticker: str,
        count: int,
        holding_side: str,
        limit_price: float,
    ) -> dict:
        """
        Liquidate an existing position by placing a crossing (taker) limit order.

        Unlike `place_order` (which is maker-only), this uses post_only=False so
        the order immediately crosses the existing best bid/ask and gets filled
        rather than resting in the book.

        Args:
            ticker:       Market ticker of the position to close.
            count:        Number of contracts to sell (positive integer).
            holding_side: "yes" → we hold YES contracts and want to sell them.
                          "no"  → we hold NO contracts and want to close them.
            limit_price:  Price per contract in the denomination of holding_side
                          (i.e. the YES price for YES positions, the NO price
                          for NO positions).  Set to the current best bid of the
                          held side to guarantee a taker fill.

        Mechanics:
          YES close → API side "ask" at limit_price (offering our YES for sale;
                      crosses existing YES bids).
          NO close  → API side "bid" at (1 − limit_price) i.e. the YES complement
                      (buying YES to net out our NO position; crosses existing
                      YES asks).

        Returns {"order": {...}} normalized the same way as place_order.
        """
        if holding_side == "yes":
            api_side = "ask"
            price_str = f"{limit_price:.4f}"
        else:
            # Closing NO: buy YES at the YES-ask complement so the order
            # crosses the book and fills immediately as a taker.
            api_side = "bid"
            price_str = f"{1.0 - limit_price:.4f}"

        body = {
            "ticker":                       ticker,
            "side":                         api_side,
            "count":                        str(count),
            "price":                        price_str,
            "time_in_force":                "good_till_canceled",
            "self_trade_prevention_type":   "maker",
            "post_only":                    False,   # taker fill for liquidation
        }
        raw = await self._request("POST", _ORDER_WRITE_PATH, body=body)
        if "order_id" in raw and "order" not in raw:
            return {"order": raw}
        return raw


def kalshi_credentials_present() -> bool:
    """Check if Kalshi API credentials are configured."""
    return bool(settings.KALSHI_API_KEY_ID and settings.KALSHI_PRIVATE_KEY_PATH)
