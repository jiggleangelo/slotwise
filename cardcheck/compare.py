from .models import Finding
from .reference import PARTNER_IDS, REFERENCE

THRESHOLDS = {'idle_core_c_max': 55.0, 'idle_hotspot_delta_max': 15.0, 'vram_tolerance': 0.03}
LIMITATIONS = ("This buyer-generated record does not prove the card's condition at shipment; cannot detect physical tampering "
               'such as die-stripping, re-engraving, or swapped memory; ran no load test, so faults that appear only under load '
               'are not covered; and has not been shown to be accepted by any marketplace or manufacturer.')


def _normal(value):
    return ''.join(str(value or '').split()).casefold()


def _device_id(value):
    try:
        return int(str(value), 16)
    except (ValueError, TypeError):
        return None


def compare(claims, identity, samples, baseline=None, ref_table=REFERENCE):
    out = []
    def add(rule, severity, title, expected, observed, explanation):
        out.append(Finding(rule, severity, title, expected, observed, explanation))
    add('R0', 'info', 'Limitations', None, None, LIMITATIONS)
    ref = ref_table.get(claims.model)
    trusted = bool(ref and ref.get('verified'))
    actual_id = _device_id(identity.pci_device_id)
    if not trusted:
        add('R1', 'not_checkable', 'Device identity reference', claims.model, identity.pci_device_id, 'Reference row is absent or awaits human review.')
    else:
        matches = actual_id in {_device_id(value) for value in ref['ids']}
        add('R1', 'info' if matches else 'mismatch', 'PCI device ID', sorted(ref['ids']), identity.pci_device_id,
            'Device ID is consistent with the reviewed reference.' if matches else 'Device ID does not match the claimed model.')
    id_models = {name for name, row in ref_table.items() if actual_id in {_device_id(value) for value in row['ids']}}
    name_models = {name for name in ref_table if _normal(name) in _normal(identity.name)}
    if not id_models or not name_models:
        add('R2', 'not_checkable', 'Name and device ID', sorted(id_models), identity.name, 'Name or device ID is not in the local reference table.')
    elif not id_models.intersection(name_models):
        add('R2', 'mismatch', 'Name and device ID', sorted(id_models), identity.name, 'consistent with a modified BIOS/name; confirm with seller')
    else:
        add('R2', 'info', 'Name and device ID', sorted(id_models), identity.name, 'Displayed name and device ID are consistent.')
    expected_gb = ref['vram_gb'] if trusted else claims.vram_gb
    difference = abs(identity.vram_total_mb - expected_gb * 1024) / (expected_gb * 1024)
    bad = difference > THRESHOLDS['vram_tolerance'] or (trusted and claims.vram_gb != expected_gb)
    add('R3', 'mismatch' if bad else 'info' if trusted else 'not_checkable', 'VRAM (heuristic tolerance)',
        f'{expected_gb} GB', f'claim {claims.vram_gb} GB; reported {identity.vram_total_mb} MB',
        'Reported VRAM and claim must agree with the reviewed reference within the 3% heuristic tolerance.' if trusted else
        'Compared with the claim using a 3% heuristic tolerance; the reference row awaits human review.')
    partner = PARTNER_IDS.get(claims.board_partner.strip().upper())
    vendor = identity.pci_subsystem_id.split(':', 1)[0].casefold() if identity.pci_subsystem_id else None
    severity = 'not_checkable' if not partner or vendor is None else 'info' if partner.casefold() == vendor else 'warning'
    add('R4', severity, 'Board partner', claims.board_partner, identity.pci_subsystem_id,
        'Partner identifier unavailable or unknown.' if severity == 'not_checkable' else
        'Subsystem vendor is consistent with the claimed board partner.' if severity == 'info' else 'Cooler swaps and OEM cards exist; ask the seller.')
    if ref and identity.pcie_max_gen is not None and identity.pcie_max_width is not None:
        gen, width = ref['pcie_gen'], ref.get('pcie_width', 16)
        bad = identity.pcie_max_gen < gen or identity.pcie_max_width < width
        add('R5', 'warning' if bad else 'info', 'Maximum PCIe link', f'Gen {gen} x{width}',
            f'Gen {identity.pcie_max_gen} x{identity.pcie_max_width}',
            'could be slot/riser; retest in primary x16 slot' if bad else 'Maximum link capability is consistent with the local reference.')
    else:
        add('R5', 'not_checkable', 'Maximum PCIe link', None, None, 'Maximum link capability is unavailable.')
    downshift = [(s.pcie_cur_gen, s.pcie_cur_width) for s in samples if
                 (s.pcie_cur_gen is not None and identity.pcie_max_gen is not None and s.pcie_cur_gen < identity.pcie_max_gen) or
                 (s.pcie_cur_width is not None and identity.pcie_max_width is not None and s.pcie_cur_width < identity.pcie_max_width)]
    if downshift:
        add('R5', 'info', 'Current PCIe link at idle', None, sorted(set(downshift), key=str), 'Current PCIe link downshift at idle is normal power management; it is not a fault diagnosis.')
    core = [s.core_temp_c for s in samples if s.core_temp_c is not None]
    deltas = [s.hotspot_c - s.core_temp_c for s in samples if s.hotspot_c is not None and s.core_temp_c is not None]
    for rule, title, values, threshold, warning in (
        ('R6', 'Idle core temperature', core, THRESHOLDS['idle_core_c_max'], 'High idle temperature is a heuristic, not a diagnosis.'),
        ('R7', 'Hotspot delta', deltas, THRESHOLDS['idle_hotspot_delta_max'], 'may indicate paste/pad issues; heuristic')):
        if not values:
            add(rule, 'not_checkable', title, None, None, title + ' is unavailable.')
        else:
            mean = sum(values) / len(values)
            add(rule, 'warning' if mean > threshold else 'info', title + ' (heuristic)', f'≤ {threshold} °C', round(mean, 1),
                warning if mean > threshold else 'Average is within the heuristic threshold.')
    readable = [s.throttle_reasons for s in samples if s.throttle_reasons is not None]
    if not readable:
        add('R8', 'not_checkable', 'Idle clock-event reasons', None, None, 'Clock-event reasons are unavailable.')
    else:
        reasons = sorted({reason for values in readable for reason in values if reason != 'GpuIdle'})
        severe = bool({'HwSlowdown', 'HwThermalSlowdown'}.intersection(reasons))
        add('R8', 'warning' if severe else 'info', 'Idle clock-event reasons', 'GpuIdle or no events', reasons,
            'Hardware slowdown reasons deserve investigation.' if severe else 'Additional idle events were observed.' if reasons else 'No additional clock-event reasons were observed.')
        if len(readable) < len(samples):
            add('R8', 'not_checkable', 'Partial clock-event coverage', len(samples), len(readable), 'Some samples lack clock-event reasons.')
    if claims.serial:
        equal = _normal(claims.serial) == _normal(identity.serial)
        add('R9', 'not_checkable' if identity.serial is None else 'info' if equal else 'mismatch', 'Listing serial', claims.serial, identity.serial,
            'The card does not expose a serial.' if identity.serial is None else 'Listing and exposed serials are consistent after case and whitespace normalization.' if equal else 'The exposed serial differs from the listing.')
    if baseline:
        for key, actual, severity in (('serial', identity.serial, 'mismatch'), ('pci_device_id', identity.pci_device_id, 'mismatch'),
                                      ('vram_total_mb', identity.vram_total_mb, 'mismatch'), ('vbios_version', identity.vbios_version, 'warning')):
            if key not in baseline.values:
                continue
            expected = baseline.values[key]
            equal = _normal(expected) == _normal(actual) if key in {'serial', 'vbios_version'} else _device_id(expected) == _device_id(actual) if key == 'pci_device_id' else expected == actual
            add('R10', 'not_checkable' if actual is None else 'info' if equal else severity, 'Seller baseline: ' + key, expected, actual,
                'The card does not expose this field.' if actual is None else 'Consistent with seller-supplied file (unverified).' if equal else
                "Different card than the seller's file describes." if severity == 'mismatch' else 'BIOS could have been updated since the seller captured the file.')
        if baseline.ignored_keys:
            add('R10', 'info', 'Ignored baseline keys', None, baseline.ignored_keys, 'Unknown keys were safely ignored.')
    if claims.vbios_version:
        equal = _normal(claims.vbios_version) == _normal(identity.vbios_version)
        add('R11', 'not_checkable' if identity.vbios_version is None else 'info' if equal else 'warning', 'Listing BIOS', claims.vbios_version, identity.vbios_version,
            'The card does not expose its BIOS version.' if identity.vbios_version is None else 'Listing and reported BIOS versions are consistent.' if equal else 'The reported BIOS differs from the listing.')
    return out


def summary(findings):
    counts = {key: sum(f.severity == key for f in findings) for key in ('mismatch', 'warning', 'not_checkable')}
    return f"{counts['mismatch']} mismatches, {counts['warning']} warnings, {counts['not_checkable']} not checkable."
