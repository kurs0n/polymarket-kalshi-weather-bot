from enum import Enum


class Outcome(str, Enum):
    """Canonical YES/NO outcome used by signals, trades, and settlement."""

    YES = "yes"
    NO = "no"
