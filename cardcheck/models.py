from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class ListingClaims:
    model: str
    vram_gb: int
    board_partner: str
    serial: str | None = None
    vbios_version: str | None = None
    notes: str = ''


@dataclass
class CardIdentity:
    name: str
    pci_device_id: str
    pci_subsystem_id: str | None
    vram_total_mb: int
    vbios_version: str | None
    serial: str | None
    driver_version: str | None
    pcie_max_gen: int | None
    pcie_max_width: int | None


@dataclass
class Sample:
    core_temp_c: float | None
    hotspot_c: float | None
    mem_temp_c: float | None
    power_w: float | None
    fan_pct: float | None
    sm_clock_mhz: int | None
    mem_clock_mhz: int | None
    pcie_cur_gen: int | None
    pcie_cur_width: int | None
    throttle_reasons: list[str] | None = field(default_factory=list)


@dataclass
class Baseline:
    values: dict[str, Any]
    ignored_keys: list[str] = field(default_factory=list)
    source: str = 'seller-supplied, unverified'


@dataclass
class Finding:
    rule_id: str
    severity: str
    title: str
    expected: Any
    observed: Any
    explanation: str


@dataclass
class Record:
    data: dict[str, Any]
    sha256: str


def plain(value):
    return asdict(value) if hasattr(value, '__dataclass_fields__') else value
