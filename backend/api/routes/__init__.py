from .system import router as system_router
from .dashboard import router as dashboard_router
from .trading import router as trading_router
from .weather import router as weather_router
from .bot import router as bot_router
from .calibration import router as calibration_router
from .integrations import router as integrations_router

__all__ = [
    "system_router",
    "dashboard_router",
    "trading_router",
    "weather_router",
    "bot_router",
    "calibration_router",
    "integrations_router",
]
