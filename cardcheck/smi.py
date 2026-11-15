import csv
import io
import json
import re
from dataclasses import fields
from .models import CardIdentity, Sample
from .record import validate_digest
from .telemetry import CLOCK_REASONS

MAX_BYTES = 256 * 1024
MAX_LINES = 5000
MAX_ROWS = 600
MISSING = {'', 'n/a', 'not supported', 'unknown error', 'insufficient permissions'}


class SmiError(ValueError):
    pass


def decode_text(data):
    """Decode UTF-8 or BOM-marked UTF-16 exports, bounded before decoding."""
    if len(data) > MAX_BYTES:
        raise SmiError('A pasted or uploaded field exceeds 256 KB.')
    encoding = 'utf-16' if data.startswith((b'\xff\xfe', b'\xfe\xff')) else 'utf-8-sig'
    try:
        text = data.decode(encoding)
    except UnicodeError as exc:
        raise SmiError('Uploads must be UTF-8 or BOM-marked UTF-16 text.') from exc
    if len(text.encode('utf-8')) > MAX_BYTES:
        raise SmiError('Decoded input exceeds 256 KB.')
    return text


def _guard(text):
    if not isinstance(text, str):
        raise SmiError('Device data must be text.')
    if len(text.encode('utf-8')) > MAX_BYTES:
        raise SmiError('Device input exceeds 256 KB.')
    if len(text.splitlines()) > MAX_LINES:
        raise SmiError('Device input exceeds 5,000 lines.')
    if not text.strip():
        raise SmiError('Device input is empty.')


def _none(value):
    if value is None:
        return None
    value = str(value).strip()
    marker = value.casefold()
    if marker.startswith('[') and marker.endswith(']'):
        marker = marker[1:-1].strip()
    return None if marker in MISSING else value[:128]


def _number(value, low, high, label, integer=False):
    value = _none(value)
    if value is None:
        return None
    match = re.search(r'[-+]?\d+(?:\.\d+)?', value.replace(',', ''))
    if not match:
        raise SmiError(f'{label} is not a number.')
    result = float(match.group())
    if not low <= result <= high:
        raise SmiError(f'{label} is outside the supported range.')
    return int(result) if integer else result


def _device(value):
    value = _none(value)
    if value is None:
        return None
    match = re.search(r'(?:0x)?([0-9a-fA-F]{4,8})', value)
    if not match:
        raise SmiError('PCI device ID is invalid.')
    digits = match.group(1)
    if len(digits) == 8:
        digits = digits[:4]
    return '0x' + digits.lower()


def _subsystem(value):
    value = _none(value)
    if value is None:
        return None
    if re.fullmatch(r'[0-9a-fA-F]{4}:[0-9a-fA-F]{4}', value):
        return value.lower()
    match = re.search(r'(?:0x)?([0-9a-fA-F]{8})', value)
    if not match:
        raise SmiError('PCI subsystem ID is invalid.')
    raw = int(match.group(1), 16)
    return f'{raw & 0xffff:04x}:{raw >> 16:04x}'


def _mask(value):
    value = _none(value)
    if value is None:
        return None
    try:
        mask = int(value, 0)
    except ValueError as exc:
        raise SmiError('Clock-event reason mask is invalid.') from exc
    labels = [label for bit, label in CLOCK_REASONS.items() if mask & bit]
    unknown = mask & ~sum(CLOCK_REASONS)
    if unknown:
        labels.append(f'UnknownClockEvent:0x{unknown:x}')
    return labels


def _sample(values):
    return Sample(
        _number(values.get('core_temp_c'), -20, 150, 'GPU temperature'),
        _number(values.get('hotspot_c'), -20, 150, 'hotspot temperature'),
        _number(values.get('mem_temp_c'), -20, 150, 'memory temperature'),
        _number(values.get('power_w'), 0, 2000, 'power draw'),
        _number(values.get('fan_pct'), 0, 100, 'fan speed'),
        _number(values.get('sm_clock_mhz'), 0, 10000, 'SM clock', True),
        _number(values.get('mem_clock_mhz'), 0, 10000, 'memory clock', True),
        _number(values.get('pcie_cur_gen'), 0, 10, 'current PCIe generation', True),
        _number(values.get('pcie_cur_width'), 0, 64, 'current PCIe width', True),
        values.get('throttle_reasons'),
    )


def parse_smi_q(text):
    _guard(text)
    text = text.lstrip('\ufeff')
    marker = re.compile(r'(?m)^\s*GPU\s+[0-9A-Fa-f:.]+\s*$')
    matches = list(marker.finditer(text))
    if matches:
        preamble = text[:matches[0].start()]
        blocks = [text[item.end():matches[index + 1].start() if index + 1 < len(matches) else len(text)]
                  for index, item in enumerate(matches)]
    else:
        preamble, blocks = '', [text]
    blocks = [item for item in blocks if item.strip()]
    if not blocks:
        raise SmiError('The nvidia-smi -q text does not contain a GPU block.')
    notes = []
    if len(blocks) > 1:
        notes.append(f'Used GPU index 0; ignored {len(blocks) - 1} additional GPU block(s).')
    flat, stack, reasons = {}, [], []
    reason_names = {
        'idle': 'GpuIdle', 'applications clocks setting': 'ApplicationsClocksSetting',
        'sw power cap': 'SwPowerCap', 'hw slowdown': 'HwSlowdown', 'sync boost': 'SyncBoost',
        'sw thermal slowdown': 'SwThermalSlowdown', 'hw thermal slowdown': 'HwThermalSlowdown',
        'hw power brake slowdown': 'HwPowerBrakeSlowdown', 'display clock setting': 'DisplayClockSetting',
    }
    reason_section_seen = False
    for raw in blocks[0].splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        indent = len(raw) - len(raw.lstrip())
        while stack and stack[-1][0] >= indent:
            stack.pop()
        if ':' in stripped:
            key, value = (part.strip() for part in stripped.split(':', 1))
        else:
            key, value = stripped, ''
        if not key or not value and set(key) <= {'=', '-'}:
            continue
        if key.casefold() in {'clocks event reasons', 'clocks throttle reasons'} and not value:
            reason_section_seen = True
        if value:
            path = ' > '.join([item[1] for item in stack] + [key])
            flat[path.casefold()] = value
            in_reasons = any(item[1].casefold() in {'clocks event reasons', 'clocks throttle reasons'} for item in stack)
            if in_reasons and value.casefold() == 'active':
                reasons.append(reason_names.get(key.casefold(), 'Unknown:' + key[:120]))
        else:
            stack.append((indent, key))
    preamble_driver = None
    for raw in preamble.splitlines():
        match = re.match(r'^\s*Driver Version\s*:\s*(.*?)\s*$', raw, re.I)
        if match:
            preamble_driver = match.group(1)
            break
    def get(*suffixes):
        for suffix in suffixes:
            for key, value in flat.items():
                if key.endswith(suffix.casefold()):
                    return value
        return None
    name = _none(get('product name'))
    did = _device(get('pci > device id', 'device id'))
    memory = _number(get('fb memory usage > total', 'total'), 0, 262144, 'VRAM', True)
    if not name or not did or memory is None:
        raise SmiError('The nvidia-smi -q text is missing Product Name, PCI Device Id, or FB Memory Total.')
    identity = CardIdentity(name, did, _subsystem(get('sub system id')), memory,
                            _none(get('vbios version')), _none(get('serial number')),
                            _none(get('driver version') or preamble_driver),
                            _number(get('pcie generation > max'), 0, 10, 'maximum PCIe generation', True),
                            _number(get('link width > max'), 0, 64, 'maximum PCIe width', True))
    sample = _sample({'core_temp_c': get('gpu current temp'), 'hotspot_c': get('gpu hotspot temp'),
                      'power_w': get('power readings > power draw', 'power draw'), 'fan_pct': get('fan speed'),
                      'sm_clock_mhz': get('clocks > graphics'), 'mem_clock_mhz': get('clocks > memory'),
                      'pcie_cur_gen': get('pcie generation > current'), 'pcie_cur_width': get('link width > current'),
                      'throttle_reasons': reasons if reason_section_seen else None})
    return identity, [sample], notes


def parse_smi_csv(text):
    _guard(text)
    try:
        rows = list(csv.reader(io.StringIO(text.lstrip('\ufeff')), skipinitialspace=True))
    except csv.Error as exc:
        raise SmiError('Device CSV is malformed.') from exc
    if len(rows) < 2:
        raise SmiError('Device CSV needs a header and at least one data row.')
    def clean(value):
        return re.sub(r'\s*(?:\[[^]]+\]|%)\s*$', '', value.strip()).strip().casefold()
    headers = [clean(value) for value in rows[0]]
    if not {'name', 'pci.device_id', 'memory.total'}.issubset(headers):
        raise SmiError('CSV requires name, pci.device_id, and memory.total columns.')
    if len(set(headers)) != len(headers):
        raise SmiError('Device CSV has repeated columns.')
    data_rows = [row for row in rows[1:] if any(cell.strip() for cell in row)]
    if not data_rows:
        raise SmiError('Device CSV needs at least one data row.')
    notes = []
    if len(data_rows) > MAX_ROWS:
        notes.append(f'Kept 600 samples and dropped {len(data_rows) - MAX_ROWS} additional rows.')
        data_rows = data_rows[:MAX_ROWS]
    mapped = []
    for row in data_rows:
        if len(row) > len(headers):
            raise SmiError('Device CSV has too many values in a row.')
        row += [''] * (len(headers) - len(row))
        mapped.append(dict(zip(headers, row)))
    first = mapped[0]
    identity = CardIdentity(_none(first['name']), _device(first['pci.device_id']),
                            _subsystem(first.get('pci.sub_device_id')),
                            _number(first['memory.total'], 0, 262144, 'VRAM', True),
                            _none(first.get('vbios_version')), _none(first.get('serial')), _none(first.get('driver_version')),
                            _number(first.get('pcie.link.gen.max'), 0, 10, 'maximum PCIe generation', True),
                            _number(first.get('pcie.link.width.max'), 0, 64, 'maximum PCIe width', True))
    if not identity.name or not identity.pci_device_id or identity.vram_total_mb is None:
        raise SmiError('CSV requires readable name, PCI device ID, and memory total values.')
    samples = []
    for row in mapped:
        event = row.get('clocks_event_reasons.active', row.get('clocks_throttle_reasons.active'))
        samples.append(_sample({'core_temp_c': row.get('temperature.gpu'), 'power_w': row.get('power.draw'),
                                'fan_pct': row.get('fan.speed'), 'sm_clock_mhz': row.get('clocks.sm'),
                                'mem_clock_mhz': row.get('clocks.mem'), 'pcie_cur_gen': row.get('pcie.link.gen.current'),
                                'pcie_cur_width': row.get('pcie.link.width.current'),
                                'throttle_reasons': _mask(event)}))
    return identity, samples, notes


def _unique(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise SmiError('Record contains a duplicate key.')
        out[key] = value
    return out


def _depth(value, level=1):
    if level > 6:
        raise SmiError('Record nesting is too deep.')
    if isinstance(value, dict):
        for child in value.values():
            _depth(child, level + 1)
    elif isinstance(value, list):
        for child in value:
            _depth(child, level + 1)


def _record_identity(value):
    if not isinstance(value, dict):
        raise SmiError('Record identity is invalid.')
    required = {item.name for item in fields(CardIdentity)}
    if not required.issubset(value):
        raise SmiError('Record identity is incomplete.')
    identity = CardIdentity(**{key: value[key] for key in required})
    if not isinstance(identity.name, str) or len(identity.name) > 128:
        raise SmiError('Record identity name is invalid.')
    identity.pci_device_id = _device(identity.pci_device_id)
    identity.pci_subsystem_id = _subsystem(identity.pci_subsystem_id)
    identity.vram_total_mb = _number(str(identity.vram_total_mb), 0, 262144, 'VRAM', True)
    for key in ('vbios_version', 'serial', 'driver_version'):
        value = getattr(identity, key)
        if value is not None and (not isinstance(value, str) or len(value) > 128):
            raise SmiError(f'Record {key} is invalid.')
    return identity


def parse_record_json(text):
    _guard(text)
    try:
        obj = json.loads(text.lstrip('\ufeff'), object_pairs_hook=_unique)
    except SmiError:
        raise
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise SmiError('Record is not valid JSON.') from exc
    _depth(obj)
    if not isinstance(obj, dict) or obj.get('schema') != 'cardcheck.record/1':
        raise SmiError('JSON schema must be cardcheck.record/1.')
    identity = _record_identity(obj.get('identity'))
    raw_samples = obj.get('samples')
    if not isinstance(raw_samples, list) or not raw_samples:
        raise SmiError('Record samples are missing.')
    notes = ['Imported record digest is valid.' if validate_digest(obj) else 'Imported record digest is missing or invalid.',
             'Imported findings were discarded and recomputed.']
    if len(raw_samples) > MAX_ROWS:
        notes.append(f'Kept 600 samples and dropped {len(raw_samples) - MAX_ROWS} additional rows.')
    names = {item.name for item in fields(Sample)}
    samples = []
    for raw in raw_samples[:MAX_ROWS]:
        if not isinstance(raw, dict) or not names.issubset(raw):
            raise SmiError('A record sample is incomplete.')
        reasons = raw.get('throttle_reasons')
        if reasons is not None and (not isinstance(reasons, list) or any(not isinstance(x, str) or len(x) > 128 for x in reasons)):
            raise SmiError('Record clock-event reasons are invalid.')
        samples.append(_sample({**raw, 'throttle_reasons': reasons}))
    return identity, samples, notes


def detect(text):
    _guard(text)
    text = text.lstrip('\ufeff')
    stripped = text.lstrip()
    if stripped.startswith('{'):
        return parse_record_json(text)
    first = stripped.splitlines()[0].casefold()
    if 'name,' in first or 'pci.device_id' in first:
        return parse_smi_csv(text)
    return parse_smi_q(text)
