from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class Forecast:
    signal: str
    up_probability: float
    down_probability: float
    explanation: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def make_forecast(up_probability: float) -> Forecast:
    up = min(1.0, max(0.0, float(up_probability)))
    down = 1.0 - up
    if up >= 0.60:
        signal = "LIKELY_UP"
        explanation = (
            f"The model estimates a {up * 100:.1f}% chance of finishing Up, "
            "which meets the 60% Likely Up threshold."
        )
    elif up <= 0.40:
        signal = "LIKELY_DOWN"
        explanation = (
            f"The model estimates a {down * 100:.1f}% chance of finishing Down, "
            "which meets the 60% Likely Down threshold."
        )
    else:
        signal = "UNCERTAIN"
        explanation = (
            f"The model estimates {up * 100:.1f}% Up and {down * 100:.1f}% Down; "
            "neither outcome reaches 60%."
        )
    return Forecast(signal, up, down, explanation)


def forecast_label(signal: str | None) -> str:
    return {
        "LIKELY_UP": "Likely Up",
        "UNCERTAIN": "Uncertain",
        "LIKELY_DOWN": "Likely Down",
    }.get(str(signal), "Uncertain")
