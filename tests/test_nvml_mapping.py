import ctypes
import ntpath
import unittest
from dataclasses import replace
from unittest.mock import patch
from cardcheck import telemetry as t
from cardcheck.baseline import parse_baseline
from cardcheck.compare import compare
from cardcheck.listing import parse_listing


class Function:
    def __init__(self, implementation):
        self.implementation = implementation
        self.calls = 0

    def __call__(self, *args):
        self.calls += 1
        return self.implementation(*args)


class Driver:
    def __init__(self):
        self.nvmlInit_v2 = Function(lambda: 0)
        self.nvmlShutdown = Function(lambda: 0)
        self.nvmlDeviceGetHandleByIndex_v2 = Function(lambda index, pointer: self.write(pointer, t.H, 123))
        self.nvmlDeviceGetPciInfo_v3 = Function(self.pci)
        self.nvmlDeviceGetMemoryInfo = Function(self.memory)
        for symbol, value in [('nvmlDeviceGetName', b'NVIDIA GeForce RTX 3090'), ('nvmlDeviceGetVbiosVersion', b'94.02'), ('nvmlSystemGetDriverVersion', b'555.1')]:
            setattr(self, symbol, Function(lambda *args, value=value: self.text(args[-2], value)))
        self.nvmlDeviceGetSerial = Function(lambda *args: 3)
        values = {'nvmlDeviceGetTemperature': 42, 'nvmlDeviceGetPowerUsage': 32000,
                  'nvmlDeviceGetFanSpeed': 30, 'nvmlDeviceGetCurrPcieLinkGeneration': 1,
                  'nvmlDeviceGetCurrPcieLinkWidth': 16, 'nvmlDeviceGetMaxPcieLinkGeneration': 4,
                  'nvmlDeviceGetMaxPcieLinkWidth': 16}
        for symbol, value in values.items():
            setattr(self, symbol, Function(lambda *args, value=value: self.write(args[-1], t.U, value)))
        self.nvmlDeviceGetClockInfo = Function(lambda handle, kind, pointer: self.write(pointer, t.U, 210 if kind == 1 else 405))
        self.nvmlDeviceGetCurrentClocksEventReasons = Function(lambda *args: 3)
        self.nvmlDeviceGetCurrentClocksThrottleReasons = Function(lambda handle, pointer: self.write(pointer, ctypes.c_ulonglong, 65))

    @staticmethod
    def write(pointer, kind, value):
        ctypes.cast(pointer, ctypes.POINTER(kind)).contents.value = value
        return 0

    @staticmethod
    def text(buffer, value):
        buffer.value = value
        return 0

    @staticmethod
    def pci(handle, pointer):
        value = ctypes.cast(pointer, ctypes.POINTER(t.PciInfo)).contents
        value.pciDeviceId = 0x220410DE
        value.pciSubSystemId = 0x30903842
        return 0

    @staticmethod
    def memory(handle, pointer):
        ctypes.cast(pointer, ctypes.POINTER(t.MemoryInfo)).contents.total = 24 * 1024 ** 3
        return 0


class NvmlMappingTests(unittest.TestCase):
    def test_mapping_fallback_capabilities_and_secure_loading(self):
        dll = Driver()
        with patch.object(t.ctypes, 'WinDLL', return_value=dll, create=True) as loader:
            source = t.NvmlTelemetry()
            try:
                identity, sample = source.identity(), source.sample()
                self.assertEqual((identity.pci_device_id, identity.pci_subsystem_id, identity.vram_total_mb), ('0x2204', '3842:3090', 24576))
                self.assertEqual((identity.name, identity.vbios_version, identity.driver_version), ('NVIDIA GeForce RTX 3090', '94.02', '555.1'))
                self.assertEqual((sample.power_w, sample.sm_clock_mhz, sample.mem_clock_mhz), (32, 210, 405))
                self.assertEqual((sample.core_temp_c, sample.fan_pct, sample.pcie_cur_gen, sample.pcie_cur_width), (42, 30, 1, 16))
                self.assertEqual(sample.throttle_reasons, ['GpuIdle', 'HwThermalSlowdown'])
                self.assertIsNone(identity.serial)
                self.assertIsNone(sample.hotspot_c)
                caps = source.capabilities()
                self.assertNotIn('serial', caps)
                self.assertNotIn('hotspot_c', caps)
                self.assertNotIn('mem_temp_c', caps)
                self.assertIn('power_w', caps)
                self.assertEqual(dll.nvmlDeviceGetPciInfo_v3.argtypes, list(t.NVML_SIGNATURES['nvmlDeviceGetPciInfo_v3']))
                self.assertIs(dll.nvmlDeviceGetMemoryInfo.restype, ctypes.c_int)
                self.assertTrue(ntpath.isabs(loader.call_args.args[0]))
                self.assertEqual(ntpath.basename(loader.call_args.args[0]).lower(), 'nvml.dll')
                self.assertEqual(loader.call_args.kwargs['winmode'], 0x1100)
            finally:
                source.close()
                source.close()
            self.assertEqual(dll.nvmlShutdown.calls, 1)
            with self.assertRaises(t.TelemetryUnavailable):
                source.identity()

    def test_missing_reads_are_not_false_matches_and_errors_are_fatal(self):
        dll = Driver()
        del dll.nvmlDeviceGetCurrentClocksEventReasons
        del dll.nvmlDeviceGetCurrentClocksThrottleReasons
        with patch.object(t.ctypes, 'WinDLL', return_value=dll, create=True):
            source = t.NvmlTelemetry()
            try:
                identity, sample = source.identity(), source.sample()
                self.assertIsNone(sample.throttle_reasons)
                claims = parse_listing({'model': 'RTX 3090', 'vram_gb': '24', 'board_partner': 'EVGA', 'serial': 'SELLER'})
                baseline = parse_baseline('{"schema":"cardcheck.baseline/1","serial":"SELLER"}')
                findings = compare(claims, identity, [sample], baseline)
                self.assertTrue({'R7', 'R8', 'R9', 'R10'} <= {f.rule_id for f in findings if f.severity == 'not_checkable'})
                dll.nvmlDeviceGetPowerUsage = Function(lambda *args: 15)
                with self.assertRaises(t.TelemetryUnavailable):
                    source.sample()
            finally:
                source.close()
        with self.assertRaises(t.TelemetryUnavailable):
            t.NvmlTelemetry('nvml.dll')

    def test_name_id_rule_is_independent_of_listing(self):
        source = t.FakeTelemetry()
        identity = replace(source.identity(), name='NVIDIA GeForce RTX 4090')
        claims = parse_listing({'model': 'RTX 3090', 'vram_gb': '24', 'board_partner': 'EVGA'})
        findings = compare(claims, identity, [source.sample()])
        self.assertIn('R2', {f.rule_id for f in findings if f.severity == 'mismatch'})


if __name__ == '__main__':
    unittest.main()
