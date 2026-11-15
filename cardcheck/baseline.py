import json
from .models import Baseline

MAX_BYTES = 32 * 1024
FIELDS = {"pci_device_id", "vram_total_mb", "vbios_version", "serial", "captured_utc", "idle_core_c"}


class BaselineError(ValueError):
    pass


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise BaselineError("Baseline contains a duplicate key.")
        result[key] = value
    return result


def _depth(value, level=1):
    if level > 4:
        raise BaselineError("Baseline nesting is too deep.")
    if isinstance(value, dict):
        for child in value.values():
            _depth(child, level + 1)
    elif isinstance(value, list):
        for child in value:
            _depth(child, level + 1)


def parse_baseline(raw: bytes | str) -> Baseline:
    data = raw.encode("utf-8") if isinstance(raw, str) else raw
    if len(data) > MAX_BYTES:
        raise BaselineError("Baseline exceeds 32 KB.")
    try:
        obj = json.loads(data.decode("utf-8"), object_pairs_hook=_unique)
    except BaselineError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise BaselineError("Baseline is not valid UTF-8 JSON.") from exc
    if not isinstance(obj, dict) or obj.get("schema") != "cardcheck.baseline/1":
        raise BaselineError("Baseline schema must be cardcheck.baseline/1.")
    _depth(obj)
    ignored = sorted(set(obj) - FIELDS - {"schema"})
    values = {key: obj[key] for key in FIELDS if key in obj}
    for key in ("pci_device_id", "vbios_version", "serial", "captured_utc"):
        if key in values and (not isinstance(values[key], str) or len(values[key]) > (10 if key == "pci_device_id" else 64 if key in {"vbios_version", "serial"} else 40)):
            raise BaselineError(f"Baseline field {key} has the wrong type or length.")
    if "pci_device_id" in values:
        try:
            int(values["pci_device_id"], 16)
        except ValueError as exc:
            raise BaselineError("Baseline PCI device ID must be hexadecimal.") from exc
    if "vram_total_mb" in values and (isinstance(values["vram_total_mb"], bool) or not isinstance(values["vram_total_mb"], int) or not 0 <= values["vram_total_mb"] <= 262144):
        raise BaselineError("Baseline VRAM has the wrong type or range.")
    if "idle_core_c" in values and (isinstance(values["idle_core_c"], bool) or not isinstance(values["idle_core_c"], (int, float)) or not -20 <= values["idle_core_c"] <= 120):
        raise BaselineError("Baseline idle temperature has the wrong type or range.")
    return Baseline(values, ignored)
