import ctypes
import ntpath
import os
from abc import ABC, abstractmethod
from copy import deepcopy
from .models import CardIdentity, Sample

NVML_ALLOWLIST = {
    'nvmlInit_v2', 'nvmlShutdown', 'nvmlDeviceGetHandleByIndex_v2',
    'nvmlDeviceGetName', 'nvmlDeviceGetPciInfo_v3', 'nvmlDeviceGetMemoryInfo',
    'nvmlDeviceGetVbiosVersion', 'nvmlDeviceGetSerial', 'nvmlDeviceGetTemperature',
    'nvmlDeviceGetPowerUsage', 'nvmlDeviceGetFanSpeed', 'nvmlDeviceGetClockInfo',
    'nvmlDeviceGetCurrPcieLinkGeneration', 'nvmlDeviceGetCurrPcieLinkWidth',
    'nvmlDeviceGetMaxPcieLinkGeneration', 'nvmlDeviceGetMaxPcieLinkWidth',
    'nvmlDeviceGetCurrentClocksEventReasons', 'nvmlDeviceGetCurrentClocksThrottleReasons',
    'nvmlSystemGetDriverVersion',
}


class PciInfo(ctypes.Structure):
    _fields_ = [('busIdLegacy', ctypes.c_char * 16), ('domain', ctypes.c_uint),
                ('bus', ctypes.c_uint), ('device', ctypes.c_uint),
                ('pciDeviceId', ctypes.c_uint), ('pciSubSystemId', ctypes.c_uint),
                ('busId', ctypes.c_char * 32)]


class MemoryInfo(ctypes.Structure):
    _fields_ = [('total', ctypes.c_ulonglong), ('free', ctypes.c_ulonglong),
                ('used', ctypes.c_ulonglong)]


H = ctypes.c_void_p
U = ctypes.c_uint
P = ctypes.POINTER
NVML_SIGNATURES = {
    'nvmlInit_v2': (), 'nvmlShutdown': (),
    'nvmlDeviceGetHandleByIndex_v2': (U, P(H)),
    'nvmlDeviceGetPciInfo_v3': (H, P(PciInfo)),
    'nvmlDeviceGetMemoryInfo': (H, P(MemoryInfo)),
    'nvmlSystemGetDriverVersion': (P(ctypes.c_char), U),
    'nvmlDeviceGetTemperature': (H, U, P(U)),
    'nvmlDeviceGetClockInfo': (H, U, P(U)),
    'nvmlDeviceGetCurrentClocksEventReasons': (H, P(ctypes.c_ulonglong)),
    'nvmlDeviceGetCurrentClocksThrottleReasons': (H, P(ctypes.c_ulonglong)),
}
for _symbol in ('nvmlDeviceGetName', 'nvmlDeviceGetVbiosVersion', 'nvmlDeviceGetSerial'):
    NVML_SIGNATURES[_symbol] = (H, P(ctypes.c_char), U)
for _symbol in ('nvmlDeviceGetPowerUsage', 'nvmlDeviceGetFanSpeed',
                'nvmlDeviceGetCurrPcieLinkGeneration', 'nvmlDeviceGetCurrPcieLinkWidth',
                'nvmlDeviceGetMaxPcieLinkGeneration', 'nvmlDeviceGetMaxPcieLinkWidth'):
    NVML_SIGNATURES[_symbol] = (H, P(U))
CLOCK_REASONS = {1: 'GpuIdle', 2: 'ApplicationsClocksSetting', 4: 'SwPowerCap',
                 8: 'HwSlowdown', 16: 'SyncBoost', 32: 'SwThermalSlowdown',
                 64: 'HwThermalSlowdown', 128: 'HwPowerBrakeSlowdown',
                 256: 'DisplayClockSetting'}


class TelemetryUnavailable(RuntimeError):
    pass


class TelemetrySource(ABC):
    @abstractmethod
    def identity(self) -> CardIdentity: ...

    @abstractmethod
    def sample(self) -> Sample: ...

    @abstractmethod
    def capabilities(self) -> set[str]: ...


SCENARIOS = {
    'honest-3090': {
        'identity': CardIdentity('NVIDIA GeForce RTX 3090', '0x2204', '3842:3090', 24576, '94.02.42.00.01', 'DEMO-0001', 'DEMO-555', 4, 16),
        'sample': Sample(42, 51, 54, 32, 30, 210, 405, 1, 16, ['GpuIdle']),
    },
    'relabelled-4090': {
        'identity': CardIdentity('NVIDIA GeForce RTX 4090', '0x2484', '10de:4090', 8192, 'DEMO.BIOS', 'DEMO-0002', 'DEMO-555', 4, 16),
        'sample': Sample(45, 53, None, 29, 25, 210, 405, 1, 16, ['GpuIdle']),
    },
    'worn-3090': {
        'identity': CardIdentity('NVIDIA GeForce RTX 3090', '0x2204', '3842:3090', 24576, '94.02.42.00.01', 'DEMO-9999', 'DEMO-555', 4, 16),
        'sample': Sample(62, 84, 88, 55, 55, 210, 405, 1, 16, ['HwThermalSlowdown']),
    },
}


class FakeTelemetry(TelemetrySource):
    def __init__(self, scenario='honest-3090'):
        if scenario not in SCENARIOS:
            raise TelemetryUnavailable('Unknown fake scenario.')
        self.data = SCENARIOS[scenario]

    def identity(self):
        return deepcopy(self.data['identity'])

    def sample(self):
        return deepcopy(self.data['sample'])

    def capabilities(self):
        return {name for item in self.data.values() for name, value in vars(item).items() if value is not None}


class NvmlTelemetry(TelemetrySource):
    """Read-only Windows NVML adapter. No load or device-setting calls."""

    def __init__(self, dll_name=None, device_index=0):
        self._initialized = False
        self._handle = None
        self._index = device_index
        root = os.environ.get('SystemRoot', r'C:\Windows')
        path = dll_name or ntpath.join(root, 'System32', 'nvml.dll')
        if not ntpath.isabs(path) or not ntpath.splitdrive(path)[0]:
            raise TelemetryUnavailable('NVML requires an absolute Windows driver DLL path.')
        try:
            # Search dependencies only beside this DLL and in System32.
            self._dll = ctypes.WinDLL(path, winmode=0x1100)
        except (AttributeError, OSError) as exc:
            raise TelemetryUnavailable('NVML is unavailable; install a supported NVIDIA driver.') from exc
        self._call('nvmlInit_v2')
        self._initialized = True

    def _call(self, symbol, *args, optional=False):
        if symbol not in NVML_ALLOWLIST or symbol not in NVML_SIGNATURES:
            raise TelemetryUnavailable('NVML operation is not on the read-only allowlist.')
        function = getattr(self._dll, symbol, None)
        if function is None:
            if optional:
                return False
            raise TelemetryUnavailable('Required NVML read function is unavailable.')
        function.argtypes = list(NVML_SIGNATURES[symbol])
        function.restype = ctypes.c_int
        status = function(*args)
        if status == 0:
            return True
        if optional and status in (3, 13):  # NOT_SUPPORTED or FUNCTION_NOT_FOUND
            return False
        raise TelemetryUnavailable(f'NVML read failed (status {status}).')

    def _device(self):
        if not self._initialized:
            raise TelemetryUnavailable('NVML source is closed.')
        if self._handle is None:
            handle = H()
            self._call('nvmlDeviceGetHandleByIndex_v2', self._index, ctypes.byref(handle))
            self._handle = handle
        return self._handle

    def _number(self, symbol, *selectors, optional=True, kind=U):
        value = kind()
        if self._call(symbol, self._device(), *selectors, ctypes.byref(value), optional=optional):
            return value.value
        return None

    def _text(self, symbol, system=False, optional=True):
        value = ctypes.create_string_buffer(256)
        args = (value, len(value)) if system else (self._device(), value, len(value))
        return value.value.decode('utf-8', errors='replace') if self._call(symbol, *args, optional=optional) else None

    def identity(self):
        handle = self._device()
        pci, memory = PciInfo(), MemoryInfo()
        self._call('nvmlDeviceGetPciInfo_v3', handle, ctypes.byref(pci))
        self._call('nvmlDeviceGetMemoryInfo', handle, ctypes.byref(memory))
        subsystem = f'{pci.pciSubSystemId & 0xffff:04x}:{pci.pciSubSystemId >> 16:04x}' if pci.pciSubSystemId else None
        return CardIdentity(
            self._text('nvmlDeviceGetName', optional=False), f'0x{pci.pciDeviceId >> 16:04x}',
            subsystem, memory.total // (1024 * 1024), self._text('nvmlDeviceGetVbiosVersion'),
            self._text('nvmlDeviceGetSerial'), self._text('nvmlSystemGetDriverVersion', system=True),
            self._number('nvmlDeviceGetMaxPcieLinkGeneration'), self._number('nvmlDeviceGetMaxPcieLinkWidth'))

    def sample(self):
        reasons = self._number('nvmlDeviceGetCurrentClocksEventReasons', kind=ctypes.c_ulonglong)
        if reasons is None:
            reasons = self._number('nvmlDeviceGetCurrentClocksThrottleReasons', kind=ctypes.c_ulonglong)
        labels = None if reasons is None else [name for bit, name in CLOCK_REASONS.items() if reasons & bit]
        if reasons is not None and reasons & ~sum(CLOCK_REASONS):
            labels.append(f'UnknownClockEvent:0x{reasons & ~sum(CLOCK_REASONS):x}')
        power = self._number('nvmlDeviceGetPowerUsage')
        return Sample(self._number('nvmlDeviceGetTemperature', 0), None, None,
                      None if power is None else power / 1000, self._number('nvmlDeviceGetFanSpeed'),
                      self._number('nvmlDeviceGetClockInfo', 1), self._number('nvmlDeviceGetClockInfo', 2),
                      self._number('nvmlDeviceGetCurrPcieLinkGeneration'),
                      self._number('nvmlDeviceGetCurrPcieLinkWidth'), labels)

    def capabilities(self):
        return {name for item in (self.identity(), self.sample()) for name, value in vars(item).items() if value is not None}

    def close(self):
        if self._initialized:
            self._initialized = False
            self._handle = None
            self._call('nvmlShutdown')
