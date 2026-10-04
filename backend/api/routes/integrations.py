from fastapi import APIRouter

from backend.api.schemas import KalshiStatusResponse

router = APIRouter()


@router.get(
    "/api/kalshi/status",
    response_model=KalshiStatusResponse,
    tags=["Integrations"],
    summary="Check Kalshi connectivity",
    description="Report whether Kalshi credentials are configured and whether an authenticated balance request succeeds.",
)
async def get_kalshi_status():
    """Test Kalshi API authentication and return connection status."""
    from backend.data.kalshi_client import KalshiClient, kalshi_credentials_present

    if not kalshi_credentials_present():
        return {
            "connected": False,
            "error": "Kalshi credentials not configured (KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY_PATH)",
        }

    try:
        client = KalshiClient()
        balance_data = await client.get_balance()
        return {
            "connected": True,
            "balance": balance_data,
        }
    except Exception as e:
        return {
            "connected": False,
            "error": str(e),
        }
