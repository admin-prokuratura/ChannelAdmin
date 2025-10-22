"""Configuration objects for the Channel Admin bot."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Dict


@dataclass(slots=True)
class PricingConfig:
    """Defines how much energy and golden cards cost."""

    energy_price_per_unit: float = 1.0
    energy_bundle_prices: Dict[int, float] = field(
        default_factory=lambda: {
            50: 3.00,
            100: 6.00,
            300: 20.0,
        }
    )
    golden_card_hourly_price: float = 1.5

    def price_for_energy(self, amount: int) -> float:
        if amount in self.energy_bundle_prices:
            return self.energy_bundle_prices[amount]
        return self.energy_price_per_unit * amount

    def price_for_golden_card(self, duration: timedelta) -> float:
        total_hours = duration.total_seconds() / 3600
        if total_hours <= 0:
            raise ValueError("Golden card duration must be positive")
        return self.golden_card_hourly_price * total_hours


@dataclass(slots=True)
class FilterConfig:
    """Configures content filtering for posts."""

    banned_words: set[str] = field(
        default_factory=lambda: {
            "хуй",
            "пизда",
            "вагина",
            "порно",
            "цп",
            "дп",
            "дрочить",
            "лизать",
            "секс",
            "долбаеб",
            "хуйня",
        }
    )
