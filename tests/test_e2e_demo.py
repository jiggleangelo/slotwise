import contextlib
import http.client
import io
import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.parse import urlencode
from unittest.mock import patch
import cli
import demo
from cardcheck.baseline import parse_baseline
from cardcheck.compare import compare
from cardcheck.listing import parse_listing
from cardcheck.observe import observe
from cardcheck.payments import FakePayments
from cardcheck.record import build_record, record_html, record_json, validate_digest
from cardcheck.telemetry import FakeTelemetry
from cardcheck.vault import FakeVault


class EndToEndTests(unittest.TestCase):
    def test_full_core_flow_without_any_network(self):
        with patch('socket.socket', side_effect=AssertionError('network forbidden')):
            for scenario in demo.SCENARIOS:
                with self.subTest(scenario=scenario):
                    model, vram, partner, serial, bios = demo.CLAIMS[scenario]
                    claims = parse_listing({'model': model, 'vram_gb': str(vram), 'board_partner': partner,
                                            'serial': serial, 'vbios_version': bios, 'notes': '<script>demo</script>'})
                    baseline = parse_baseline(json.dumps(demo.BASELINES[scenario]))
                    source, waits = FakeTelemetry(scenario), []
                    identity = source.identity()
                    samples = observe(source, 60, waits.append)
                    self.assertEqual(waits, [1] * 59)
                    record = build_record(claims, identity, samples, compare(claims, identity, samples, baseline), baseline)
                    decoded = json.loads(record_json(record))
                    self.assertTrue(validate_digest(decoded))
                    self.assertEqual(len(decoded['samples']), 60)
                    self.assertEqual(decoded['baseline']['source'], 'seller-supplied, unverified')
                    mismatches = {f['rule_id'] for f in decoded['findings'] if f['severity'] == 'mismatch'}
                    if scenario == 'honest-3090':
                        self.assertEqual(mismatches, set())
                    elif scenario == 'relabelled-4090':
                        self.assertTrue({'R1', 'R2', 'R3', 'R9', 'R10'} <= mismatches)
                    else:
                        self.assertIn('R10', mismatches)
                        self.assertTrue({'R6', 'R7', 'R8'} <= {f['rule_id'] for f in decoded['findings'] if f['severity'] == 'warning'})
                    printed = record_html(record, demo.BANNER)
                    self.assertIn(record.sha256, printed)
                    self.assertIn(decoded['created_utc'], printed)
                    self.assertIn('&lt;script&gt;', printed)
                    self.assertNotIn('<script>demo', printed)
                    decoded['claims']['notes'] = 'edited'
                    self.assertFalse(validate_digest(decoded))
            payments, vault = FakePayments(), FakeVault()
            self.assertEqual(payments.checkout_url('single'), '/pricing?clicked=single')
            self.assertEqual(payments.checkout_url('five'), '/pricing?clicked=five')
            self.assertFalse(payments.verify_webhook(b'anything', {}))
            self.assertTrue(vault.store('email', 'demo@example.invalid').startswith('vault-'))
            self.assertNotIn('demo@example.invalid', repr(vars(vault)))
            self.assertFalse(hasattr(vault, 'read'))
            with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                baseline_path = Path(directory) / 'baseline.json'
                output = Path(directory) / 'record.json'
                self.assertEqual(cli.main(['--make-baseline', '--out', str(baseline_path)]), 0)
                self.assertEqual(cli.main(['--samples', '1', '--baseline', str(baseline_path), '--out', str(output)]), 0)
                data = json.loads(output.read_bytes())
                self.assertTrue(validate_digest(data))
                self.assertIn(data['sha256'], output.with_suffix('.html').read_text(encoding='utf-8'))

    def test_http_exports_methods_and_shared_proxy(self):
        original = socket.socket.connect
        def loopback_only(sock, address):
            if address[0] != '127.0.0.1':
                raise AssertionError('outbound forbidden')
            return original(sock, address)
        server = demo.make_server('127.0.0.1', 0)
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
        thread.start()
        def request(method, path, body=None):
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
            try:
                connection.request(method, path, body, {'Content-Type': 'application/x-www-form-urlencoded'})
                response = connection.getresponse()
                return response.status, dict(response.getheaders()), response.read()
            finally:
                connection.close()
        try:
            with patch('socket.socket.connect', new=loopback_only):
                payload = urlencode({'scenario': 'honest-3090', 'model': 'RTX 3090', 'vram_gb': '24', 'board_partner': 'EVGA', 'baseline': json.dumps(demo.BASELINES['honest-3090'])})
                for _ in range(35):
                    status, headers, _ = request('POST', '/check', payload)
                    self.assertEqual(status, 303)
                self.assertEqual(demo.RATE_EVENTS, {})
                location = headers['Location']
                status, _, raw = request('GET', location + '.json')
                self.assertEqual(status, 200)
                record = json.loads(raw)
                self.assertTrue(validate_digest(record))
                status, headers, body = request('GET', location + '.html')
                self.assertEqual(status, 200)
                self.assertIn('attachment', headers['Content-Disposition'])
                self.assertIn(demo.BANNER.encode(), body)
                self.assertIn(record['sha256'].encode(), body)
                self.assertIn(record['created_utc'].encode(), body)
                for method, path, expected in [('HEAD', '/', 200), ('HEAD', '/missing', 404), ('PUT', '/', 501), ('OPTIONS', '/', 501)]:
                    status, headers, body = request(method, path)
                    self.assertEqual(status, expected)
                    self.assertEqual(headers['X-Frame-Options'], 'DENY')
                    self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
                    self.assertIn("frame-ancestors 'none'", headers['Content-Security-Policy'])
                    self.assertEqual(body, b'') if method == 'HEAD' else self.assertIn(demo.BANNER.encode(), body)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)

    def test_connection_cap_and_absolute_deadline(self):
        server = demo.make_server('127.0.0.1', 0, max_connections=1, request_deadline=0.15)
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
        thread.start()
        first = second = None
        try:
            first = socket.create_connection(server.server_address, timeout=2)
            first.sendall(b'GET / HTTP/1.0\r\nX-Slow: ')
            until = time.monotonic() + 1
            while time.monotonic() < until:
                with server._active_lock:
                    if server._active:
                        break
                time.sleep(0.005)
            with server._active_lock:
                self.assertEqual(len(server._active), 1)
            second = socket.create_connection(server.server_address, timeout=2)
            try:
                self.assertEqual(second.recv(1), b'')
            except ConnectionResetError:
                pass
            # The partial request is disconnected by the wall-clock deadline,
            # far sooner than the handler's ten-second socket timeout.
            try:
                self.assertEqual(first.recv(1), b'')
            except ConnectionResetError:
                pass
            until = time.monotonic() + 1
            while time.monotonic() < until:
                with server._active_lock:
                    if not server._active:
                        break
                time.sleep(0.005)
            connection = http.client.HTTPConnection(*server.server_address, timeout=2)
            try:
                connection.request('GET', '/healthz')
                response = connection.getresponse()
                self.assertEqual((response.status, response.read()), (200, b'ok'))
            finally:
                connection.close()
        finally:
            if first:
                first.close()
            if second:
                second.close()
            server.shutdown()
            server.server_close()
            thread.join(2)


if __name__ == '__main__':
    unittest.main()
