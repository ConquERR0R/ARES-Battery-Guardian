"""Hardware abstraction layer for firmware charge control."""
from __future__ import annotations

import abc
from dataclasses import dataclass, field

from app.utils.calculations import ThresholdLimits, ThresholdPlan, plan_thresholds


@dataclass
class ChargeConfig:
    """Firmware charge configuration as read back from the hardware."""
    mode: str                      # e.g. "Custom", "Standard", "Express", "Adaptive", "PrimAcUse"
    start: int | None = None       # custom start threshold (%), only meaningful in Custom mode
    stop: int | None = None        # custom stop threshold (%)

    @property
    def is_custom(self) -> bool:
        return self.mode.lower() == "custom"

    def matches(self, start: int, stop: int) -> bool:
        return self.is_custom and self.start == start and self.stop == stop

    def describe(self) -> str:
        if self.is_custom:
            return f"Custom: start {self.start}% / stop {self.stop}%"
        return {"standard": "Standard (charges to 100%)", "express": "ExpressCharge (charges to 100%)",
                "adaptive": "Adaptive (firmware decides)", "primacuse": "Primarily AC use (firmware decides)"
                }.get(self.mode.lower(), self.mode)

    def to_dict(self) -> dict:
        return {"mode": self.mode, "start": self.start, "stop": self.stop}

    @classmethod
    def from_dict(cls, d: dict | None) -> "ChargeConfig | None":
        if not d or "mode" not in d:
            return None
        return cls(d["mode"], d.get("start"), d.get("stop"))


@dataclass
class ControllerCapabilities:
    vendor: str
    interface: str | None                 # human-readable interface name, None = none found
    supported: bool                       # threshold control is possible on this machine
    can_read: bool = False
    can_write: bool = False
    requires_admin: bool = False
    password_required: bool | None = None
    limits: ThresholdLimits | None = None
    available_modes: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        if self.supported and self.can_write:
            return f"Supported via {self.interface}"
        if self.supported and self.can_read:
            return f"Read-only via {self.interface}"
        return "Monitoring only (no supported charge-control interface)"


@dataclass
class WriteResult:
    success: bool                 # the interface reported the write as accepted
    verified: bool                # read-back from firmware matches what we asked for
    message: str
    config_after: ChargeConfig | None = None
    needs_password: bool = False
    needs_admin: bool = False


class ChargeControlError(Exception):
    pass


class BaseChargeController(abc.ABC):
    """A charge controller talks to ONE documented manufacturer interface."""

    vendor = "Unknown"

    @abc.abstractmethod
    def probe(self) -> ControllerCapabilities:
        """Detect what this machine supports. Must not write anything."""

    @abc.abstractmethod
    def read_config(self) -> ChargeConfig | None:
        """Read the current firmware charge configuration, or None if it cannot be read."""

    @abc.abstractmethod
    def write_thresholds(self, start: int, stop: int, password: str | None = None) -> WriteResult:
        """Set custom start/stop thresholds and verify by read-back."""

    @abc.abstractmethod
    def restore_mode(self, config: ChargeConfig, password: str | None = None) -> WriteResult:
        """Restore a previously saved configuration (e.g. the factory Standard/Express mode)."""

    @property
    def capabilities(self) -> ControllerCapabilities:
        caps = getattr(self, "_caps", None)
        if caps is None:
            caps = self.probe()
            self._caps = caps
        return caps

    def plan(self, start: int, stop: int) -> ThresholdPlan | None:
        limits = self.capabilities.limits
        if limits is None:
            return None
        return plan_thresholds(start, stop, limits)
