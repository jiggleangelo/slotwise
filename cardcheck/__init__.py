"""CardCheck package initialization.

Keep the NVML allowlist synchronized with every NVML-prefixed literal used by
the telemetry implementation, including the securely loaded library filename.
"""

from . import telemetry as _telemetry

_telemetry.NVML_ALLOWLIST = frozenset(_telemetry.NVML_ALLOWLIST) | {"nvml.dll"}
