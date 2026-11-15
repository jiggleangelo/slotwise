import argparse
import json
import os
import sys
from pathlib import Path
from cardcheck.baseline import MAX_BYTES, BaselineError, parse_baseline
from cardcheck.compare import compare
from cardcheck.listing import ListingError, parse_listing
from cardcheck.observe import observe
from cardcheck.record import build_record, record_html, record_json
from cardcheck.telemetry import FakeTelemetry, NvmlTelemetry, TelemetryUnavailable


def _write_private(path, payload, force=False):
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if not force:
        flags |= os.O_EXCL
    if hasattr(os, 'O_NOFOLLOW'):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        if hasattr(os, 'fchmod'):
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, 'wb') as stream:
            descriptor = -1
            stream.write(payload)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _read_baseline(path):
    with Path(path).open('rb') as stream:
        return parse_baseline(stream.read(MAX_BYTES + 1))


def main(argv=None):
    parser = argparse.ArgumentParser(description='Read-only used-GPU delivery check; no GPU load is generated.')
    parser.add_argument('--model', default='RTX 3090')
    parser.add_argument('--vram', type=int, default=24)
    parser.add_argument('--partner', default='EVGA')
    parser.add_argument('--serial', default='')
    parser.add_argument('--bios', default='')
    parser.add_argument('--notes', default='')
    parser.add_argument('--baseline')
    parser.add_argument('--scenario', default='honest-3090')
    parser.add_argument('--source', choices=('fake', 'nvml'), default='fake')
    parser.add_argument('--samples', type=int, default=60)
    parser.add_argument('--out', default='record.json')
    parser.add_argument('--make-baseline', action='store_true')
    parser.add_argument('--force', action='store_true', help='overwrite output files')
    args = parser.parse_args(argv)
    source = None
    try:
        output = Path(args.out)
        html_output = output.with_suffix('.html')
        if not args.make_baseline and output.resolve() == html_output.resolve():
            raise ValueError('--out must name a JSON file, not an HTML file.')
        targets = [output] if args.make_baseline else [output, html_output]
        if not args.force:
            for target in targets:
                if target.exists() or target.is_symlink():
                    raise FileExistsError(str(target))
        source = FakeTelemetry(args.scenario) if args.source == 'fake' else NvmlTelemetry()
        identity = source.identity()
        if args.make_baseline:
            data = {'schema': 'cardcheck.baseline/1', 'pci_device_id': identity.pci_device_id, 'vram_total_mb': identity.vram_total_mb}
            data.update({key: value for key, value in {'vbios_version': identity.vbios_version, 'serial': identity.serial}.items() if value is not None})
            _write_private(output, (json.dumps(data, indent=2) + '\n').encode('utf-8'), args.force)
        else:
            claims = parse_listing({'model': args.model, 'vram_gb': str(args.vram), 'board_partner': args.partner,
                                    'serial': args.serial, 'vbios_version': args.bios, 'notes': args.notes})
            baseline = _read_baseline(args.baseline) if args.baseline else None
            samples = observe(source, args.samples)
            record = build_record(claims, identity, samples, compare(claims, identity, samples, baseline), baseline)
            _write_private(output, record_json(record), args.force)
            _write_private(html_output, record_html(record).encode('utf-8'), args.force)
            print(record.data['summary'])
        print('Warning: output may contain device and listing serials; keep it private.', file=sys.stderr)
        return 0
    except FileExistsError:
        print('cardcheck: output already exists; use --force to overwrite', file=sys.stderr)
        return 2
    except (BaselineError, ListingError, TelemetryUnavailable, ValueError, OSError) as exc:
        print(f'cardcheck: {exc}', file=sys.stderr)
        return 2
    finally:
        if isinstance(source, NvmlTelemetry):
            try:
                source.close()
            except TelemetryUnavailable:
                pass


if __name__ == '__main__':
    raise SystemExit(main())
