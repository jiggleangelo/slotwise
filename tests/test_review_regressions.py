import json
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, urlparse
from unittest.mock import patch

import app
from cardcheck.compare import compare
from cardcheck.listing import parse_listing
from cardcheck.record import build_record, validate_digest
from cardcheck.smi import MAX_BYTES, SmiError, decode_text, detect
from cardcheck.telemetry import FakeTelemetry
from cardcheck.workspace import Store, WorkspaceError, WorkspaceNotFound, IDLE_SECONDS, ACCOUNTING_RESERVE
from tests.test_app import AppTests, Q_TEXT


CSV = '''name, pci.device_id, pci.sub_device_id, memory.total [MiB], vbios_version, serial, driver_version, pcie.link.gen.max, pcie.link.width.max, pcie.link.gen.current, pcie.link.width.current, temperature.gpu, power.draw [W], fan.speed [%], clocks.sm [MHz], clocks.mem [MHz], clocks_event_reasons.active
NVIDIA GeForce RTX 3090, 0x220410DE, 0x30903842, 24576 MiB, [Not Supported], [Insufficient Permissions], 555.1, 4, 16, 1, 16, [Unknown Error], [Not Supported], [Insufficient Permissions], 210 MHz, 405 MHz, [Not Supported]
'''


def multipart(values, files=None):
    boundary = '----CardCheckBrowserBoundary'
    files = files or {}
    chunks = []
    for name in ('label', 'model', 'vram_gb', 'board_partner', 'serial', 'vbios_version', 'notes', 'device_text', 'device_file', 'baseline_text', 'baseline_file'):
        disposition = f'Content-Disposition: form-data; name="{name}"'
        if name.endswith('_file'):
            filename, data = files.get(name, ('', b''))
            disposition += f'; filename="{filename}"\r\nContent-Type: application/octet-stream'
        else:
            data = values.get(name, '').encode()
        chunks.append(b'--' + boundary.encode() + b'\r\n' + disposition.encode() + b'\r\n\r\n' + data + b'\r\n')
    chunks.append(b'--' + boundary.encode() + b'--\r\n')
    return b''.join(chunks), {'Content-Type': 'multipart/form-data; boundary=' + boundary}


def record():
    source = FakeTelemetry()
    claims = parse_listing({'model': 'RTX 3090', 'vram_gb': '24', 'board_partner': 'EVGA'})
    samples = [source.sample()]
    return build_record(claims, source.identity(), samples, compare(claims, source.identity(), samples)).data


class BrowserRegressionTests(unittest.TestCase):
    setUp = AppTests.setUp
    tearDown = AppTests.tearDown
    request = AppTests.request
    start = AppTests.start

    def submit(self, workspace, text=Q_TEXT, files=None, **extra):
        values = {'model': 'RTX 3090', 'vram_gb': '24', 'board_partner': 'EVGA', 'device_text': text, **extra}
        body, headers = multipart(values, files)
        return self.request('POST', workspace + '/check', body, headers)

    def test_browser_eleven_parts_empty_file_controls_and_download(self):
        workspace = self.start()
        status, headers, body = self.submit(workspace)
        self.assertEqual(status, 303, body)
        check = urlparse(headers['Location']).path
        self.assertEqual(self.request('GET', check)[0], 200)
        status, _, raw = self.request('GET', check + '.json')
        self.assertEqual(status, 200)
        self.assertTrue(validate_digest(json.loads(raw)))

    def test_multipart_rejects_excess_parts_duplicates_and_oversized_uploads(self):
        body, headers = multipart({})
        boundary = headers['Content-Type'].split('boundary=')[1].encode()
        extra = b'--' + boundary + b'\r\nContent-Disposition: form-data; name="notes"\r\n\r\nx\r\n'
        with self.assertRaises(ValueError):
            app._parse_multipart(extra + body, headers['Content-Type'])
        duplicate = body.replace(b'name="label"', b'name="model"', 1)
        with self.assertRaises(ValueError):
            app._parse_multipart(duplicate, headers['Content-Type'])
        body, headers = multipart({}, {'device_file': ('large.txt', b'x' * (MAX_BYTES + 1))})
        self.assertEqual(self.request('POST', self.start() + '/check', body, headers)[0], 400)

    def test_windows_upload_without_javascript_and_utf8_bom_json(self):
        workspace = self.start()
        for encoding in ('utf-16', 'utf-8-sig'):
            with self.subTest(encoding=encoding):
                status, _, body = self.submit(workspace, text='', files={'device_file': ('out.txt', Q_TEXT.encode(encoding))})
                self.assertEqual(status, 303, body)
        raw = json.dumps(record()).encode('utf-8-sig')
        status, _, body = self.submit(workspace, text='', files={'device_file': ('record.json', raw)})
        self.assertEqual(status, 303, body)
        baseline = json.dumps({'schema': 'cardcheck.baseline/1', 'serial': 'SELLER'}).encode('utf-16')
        self.assertEqual(self.submit(workspace, files={'baseline_file': ('seller.json', baseline)})[0], 303)

    def test_real_csv_spacing_and_unsupported_fields_end_to_end(self):
        workspace = self.start()
        status, headers, body = self.submit(workspace, CSV, serial='LISTING', vbios_version='94.02')
        self.assertEqual(status, 303, body)
        check = urlparse(headers['Location']).path
        data = json.loads(self.request('GET', check + '.json')[2])
        unchecked = {f['rule_id'] for f in data['findings'] if f['severity'] == 'not_checkable'}
        self.assertTrue({'R6', 'R7', 'R8', 'R9', 'R11'} <= unchecked)
        self.assertIsNone(data['samples'][0]['power_w'])
        self.assertIsNone(data['samples'][0]['fan_pct'])
        self.assertEqual(data['identity']['pci_subsystem_id'], '3842:3090')

    def test_notes_length_precedes_all_contact_detection(self):
        workspace = self.start()
        for notes in ('x' * 501, ' ' * 501):
            with patch('app.contains_contact', side_effect=AssertionError('must not run')) as detector:
                status, _, body = self.submit(workspace, notes=notes)
            self.assertEqual(status, 400)
            self.assertIn(b'Notes cannot exceed 500 characters', body)
            detector.assert_not_called()

    def test_unknown_and_deleted_post_targets_are_404_without_disk_growth(self):
        root = Path(self.temp.name)
        before = {str(p): p.read_bytes() for p in root.rglob('*') if p.is_file()}
        for index in range(20):
            token = f'{index:032d}'
            self.assertEqual(self.request('POST', '/w/' + token + '/delete', b'')[0], 404)
        after = {str(p): p.read_bytes() for p in root.rglob('*') if p.is_file()}
        self.assertEqual(before, after)
        workspace = self.start()
        self.assertEqual(self.request('POST', workspace + '/c/' + 'x' * 12 + '/delete', b'')[0], 404)
        self.assertEqual(self.request('POST', workspace + '/delete', b'')[0], 303)
        self.assertEqual(self.request('POST', workspace + '/delete', b'')[0], 404)
        self.assertEqual(self.submit(workspace)[0], 404)
        self.assertEqual(self.request('POST', workspace + '/c/' + 'x' * 12 + '/delete', b'')[0], 404)

    def test_workspace_discloses_retention_and_csp_uses_external_script(self):
        for path in ('/', self.start()):
            status, headers, body = self.request('GET', path)
            self.assertEqual(status, 200)
            self.assertIn(b'90 days', body)
            self.assertIn(b'24 hours', body)
            directives = headers['Content-Security-Policy'].split(';')
            script = next(d.strip() for d in directives if d.strip().startswith('script-src'))
            self.assertEqual(script, "script-src 'self'")
            self.assertIn(b'<script src="/static/app.js" defer>', body)
        self.assertEqual(self.request('GET', '/static/app.js')[0], 200)

    def test_export_retains_label_notes_sample_flag_and_record_digest(self):
        workspace = self.start()
        status, _, body = self.submit(workspace, '\ufeff' + json.dumps(record()), label='Delivery record')
        self.assertEqual(status, 303, body)
        exported = json.loads(self.request('GET', workspace + '/export')[2])
        cid = exported['check_ids'][0]
        metadata = exported['check_metadata'][cid]
        self.assertEqual(metadata['label'], 'Delivery record')
        self.assertIn('Imported findings were discarded and recomputed.', metadata['parser_notes'])
        self.assertIs(metadata['sample'], False)
        self.assertTrue(validate_digest(exported['checks'][0]))

    def test_sample_route_creates_three_dated_private_checks(self):
        status, headers, body = self.request('POST', '/sample', b'')
        self.assertEqual(status, 303, body)
        workspace = urlparse(headers['Location']).path
        exported = json.loads(self.request('GET', workspace + '/export')[2])
        self.assertEqual(len(exported['checks']), 3)
        for cid, data in zip(exported['check_ids'], exported['checks']):
            self.assertTrue(validate_digest(data))
            self.assertEqual(len(data['samples']), 60)
            self.assertTrue(data['created_utc'])
            meta = exported['check_metadata'][cid]
            self.assertTrue(meta['sample'])
            self.assertEqual(meta['parser_notes'], ['Made-up sample data.'])
            status, _, body = self.request('GET', workspace + '/c/' + cid)
            self.assertEqual(status, 200)
            self.assertIn(b'Made-up sample data.', body)


class ParserRegressionTests(unittest.TestCase):
    def test_all_missing_markers_and_old_clock_header(self):
        for marker in ('N/A', '[N/A]', '[Not Supported]', '[Unknown Error]', '[Insufficient Permissions]'):
            text = CSV
            for old in ('[Not Supported]', '[Unknown Error]', '[Insufficient Permissions]'):
                text = text.replace(old, marker)
            for header in ('clocks_event_reasons.active', 'clocks_throttle_reasons.active'):
                with self.subTest(marker=marker, header=header):
                    identity, samples, _ = detect(text.replace('clocks_event_reasons.active', header))
                    self.assertIsNone(identity.serial)
                    self.assertIsNone(identity.vbios_version)
                    self.assertIsNone(samples[0].throttle_reasons)
                    self.assertIsNone(samples[0].core_temp_c)
                    self.assertIsNone(samples[0].power_w)
                    self.assertIsNone(samples[0].fan_pct)

    def test_bom_detection_for_json_csv_and_q(self):
        for text in (json.dumps(record()), CSV, Q_TEXT):
            with self.subTest(text=text[:25]):
                identity, samples, _ = detect('\ufeff' + text)
                self.assertEqual(identity.pci_device_id, '0x2204')
                self.assertTrue(samples)
        for raw in (b'\xff\xfe' + Q_TEXT.encode('utf-16-le'), b'\xfe\xff' + Q_TEXT.encode('utf-16-be')):
            self.assertEqual(decode_text(raw), Q_TEXT)
        with self.assertRaises(SmiError):
            decode_text(b'\xff')
        with self.assertRaises(SmiError):
            decode_text(b'x' * (MAX_BYTES + 1))

    def test_csv_blank_rows_and_missing_required_values_are_safe_errors(self):
        for text in ('name, pci.device_id, memory.total\n\n', 'name, pci.device_id, memory.total\nGPU, 0x220410DE, [Not Supported]\n'):
            with self.assertRaises(SmiError):
                detect(text)

    def test_absent_q_clock_section_is_not_a_false_clean_read(self):
        _, samples, _ = detect(Q_TEXT.split('    Clocks Event Reasons')[0])
        self.assertIsNone(samples[0].throttle_reasons)


class StoreRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def envelope(self, padding=0):
        return {'record': {'created_utc': '2026-10-26', 'padding': 'x' * padding}, 'label': 'Test', 'parser_notes': [], 'sample': False}

    def assert_accounting(self):
        actual = sum(p.stat().st_size for p in self.store.root.rglob('*') if p.is_file() and p.name != 'usage.json')
        self.assertEqual(self.store._bytes, actual)
        self.assertLess((self.store.root / 'usage.json').stat().st_size, ACCOUNTING_RESERVE)

    def test_unknown_deletes_do_not_allocate_markers_or_rewrite_usage(self):
        with patch.object(self.store, '_atomic', side_effect=AssertionError('no write')):
            for index in range(50):
                with self.assertRaises(WorkspaceNotFound):
                    self.store.delete(f'{index:032d}')
        self.assertEqual(list(self.store.tomb.iterdir()), [])

    def test_hot_writes_never_walk_other_workspaces_and_ledger_survives_restart(self):
        token = self.store.create()
        other = self.store.create()
        original = Path.rglob
        def selected_only(path, pattern):
            if path in (self.store.root, self.store.ws):
                raise AssertionError('global scan on hot path')
            return original(path, pattern)
        with patch.object(Path, 'rglob', selected_only):
            cid = self.store.put_check(token, self.envelope())
            self.store.delete_check(token, cid)
            self.store.put_check(other, self.envelope())
            self.store.delete(token)
            self.store.create()
        self.assert_accounting()
        restarted = Store(self.temp.name)
        try:
            self.assertEqual(restarted._bytes, self.store._bytes)
            self.assertEqual(restarted._workspaces, self.store._workspaces)
        finally:
            restarted.close()

    def test_concurrent_writes_share_global_capacity_check(self):
        tokens = [self.store.create(), self.store.create()]
        # One ~4 KB payload plus metadata fits, two do not.
        self.store.cap = self.store._size() + 6500
        barrier = threading.Barrier(2)
        def save(token):
            barrier.wait(timeout=2)
            try:
                return self.store.put_check(token, self.envelope(4000))
            except WorkspaceError:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(save, tokens))
        self.assertEqual(sum(value is not None for value in results), 1)
        self.assertLessEqual(self.store._size(), self.store.cap)
        self.assert_accounting()

    def test_lock_identity_survives_churn_while_held(self):
        lock = self.store._lock('held')
        entered = threading.Event()
        def contender():
            with self.store._lock('held'):
                entered.set()
        with lock:
            for index in range(5000):
                self.store._lock(str(index))
            self.assertIs(lock, self.store._lock('held'))
            thread = threading.Thread(target=contender)
            thread.start()
            self.assertFalse(entered.wait(0.05))
        thread.join(2)
        self.assertTrue(entered.is_set())

    def test_reads_and_exports_refresh_idle_expiry_but_unknown_checks_do_not(self):
        for operation in ('list', 'get', 'export', 'meta'):
            with self.subTest(operation=operation):
                token = self.store.create()
                cid = self.store.put_check(token, self.envelope())
                _, path = self.store.path(token)
                meta = self.store._meta(path)
                old = datetime.fromtimestamp(time.time() - IDLE_SECONDS - 100, timezone.utc).isoformat()
                meta.update(created=old, last_used=old)
                with self.store._global:
                    self.store._atomic(path / 'meta.json', json.dumps(meta).encode())
                with self.assertRaises(WorkspaceNotFound):
                    self.store.get_check(token, 'x' * 12)
                self.assertEqual(self.store._meta(path)['last_used'], old)
                if operation == 'list':
                    self.store.list_checks(token)
                elif operation == 'get':
                    self.store.get_check(token, cid)
                elif operation == 'export':
                    self.store.export(token)
                else:
                    self.store.get_meta(token)
                self.store.sweep()
                self.assertTrue(path.exists())
                self.store.sweep(now=time.time() + IDLE_SECONDS + 1)
                with self.assertRaises(WorkspaceNotFound):
                    self.store.path(token)
        self.assert_accounting()

    def test_deleted_workspace_cannot_be_recreated_by_delayed_save(self):
        token = self.store.create()
        self.store.put_check(token, self.envelope())
        self.store.delete(token)
        with self.assertRaises(WorkspaceNotFound):
            self.store.put_check(token, self.envelope())
        self.assertEqual(len(list(self.store.tomb.iterdir())), 1)
        self.assertGreater(next(self.store.tomb.iterdir()).stat().st_size, 0)
        self.assert_accounting()

    def test_empty_workspace_expiry_and_marker_cleanup_are_accounted(self):
        token = self.store.create()
        self.store.sweep(now=time.time() + 86401)
        with self.assertRaises(WorkspaceNotFound):
            self.store.path(token)
        self.store.sweep(now=time.time() + 172802)
        self.assertEqual(list(self.store.tomb.iterdir()), [])
        self.assert_accounting()


if __name__ == '__main__':
    unittest.main()
