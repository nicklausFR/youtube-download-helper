"""Decimal download-rate units used by the interface and the media engine."""

MAX_RATE_BPS = 125_000_000  # 1000 Mbit/s
MIN_RATE_BPS = 10_000  # 10 ko/s; zero remains unlimited
RATE_UNITS = {
    "ko/s": (1_000, 0),
    "Mo/s": (1_000_000, 3),
    "Mbit/s": (125_000, 3),
}


def rate_to_bps(value, unit):
    factor, _ = RATE_UNITS[unit]
    limit = round(float(value) * factor)
    if not 0 <= limit <= MAX_RATE_BPS:
        raise ValueError("Limite de débit invalide")
    return max(MIN_RATE_BPS, limit) if limit else 0
