import contextlib
import http.client
import io
import json
import socket
import threading
import unittest
from urllib.parse import urlencode
from unittest.mock import patch
import demo
from cardcheck.record import validate_digest


class DemoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        demo.STORE.clear()
        demo.RATE_EVENTS.clear()
        cls.server = demo.make_server('127.0.0.1', 0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(2)

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=3)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def submit(self, scenario, notes=''):
        model, vram, partner, serial, bios = demo.CLAIMS[scenario]
        data = urlencode({'scenario': scenario, 'model': model, 'vram_gb': vram, 'board_partner': partner, 'serial': serial,
                          'vbios_version': bios, 'notes': notes, 'baseline': json.dumps(demo.BASELINES[scenario])})
        return self.request('POST', '/check', data, {'Content-Type': 'application/x-www-form-urlencoded'})

    def test_health_and_banner(self):
        status, headers, body = self.request('GET', '/healthz')
        self.assertEqual((status, body), (200, b'ok'))
        self.assertEqual(headers['X-Frame-Options'], 'DENY')
        status, headers, body = self.request('GET', '/')
        self.assertEqual(status, 200)
        self.assertIn(demo.BANNER.encode(), body)
        self.assertIn("frame-ancestors 'none'", headers['Content-Security-Policy'])

    def test_core_flow_all_scenarios(self):
        for scenario in demo.SCENARIOS:
            with self.subTest(scenario=scenario):
                status, headers, _ = self.submit(scenario)
                self.assertEqual(status, 303)
                location = headers['Location']
                status, _, page = self.request('GET', location)
                self.assertEqual(status, 200)
                self.assertIn(demo.BANNER.encode(), page)
                status, _, raw = self.request('GET', location + '.json')
                self.assertEqual(status, 200)
                record = json.loads(raw)
                self.assertTrue(validate_digest(record))
                mismatches = {f['rule_id'] for f in record['findings'] if f['severity'] == 'mismatch'}
                warnings = {f['rule_id'] for f in record['findings'] if f['severity'] == 'warning'}
                if scenario == 'honest-3090':
                    self.assertEqual(mismatches, set())
                    self.assertEqual(warnings, set())
                else:
                    self.assertRegex(page, rb'<tr class=[\x22\x27]mismatch[\x22\x27]>')
                    self.assertTrue({'R1', 'R2', 'R3'} <= mismatches if scenario == 'relabelled-4090' else 'R10' in mismatches)
                    if scenario == 'worn-3090':
                        self.assertTrue({'R6', 'R7', 'R8'} <= warnings)

    def test_limits_escaping_and_missing_record(self):
        self.assertEqual(self.request('POST', '/check', b'x' * 70000, {'Content-Type': 'application/x-www-form-urlencoded'})[0], 413)
        status, headers, _ = self.submit('honest-3090', '<script>alert(1)</script>')
        self.assertEqual(status, 303)
        _, _, page = self.request('GET', headers['Location'])
        self.assertNotIn(b'<script>alert', page)
        self.assertIn(b'&lt;script&gt;', page)
        self.assertEqual(self.request('GET', '/record/missing')[0], 404)
        self.assertEqual(self.submit('honest-3090', 'call 555-123-4567')[0], 400)
        self.assertEqual(self.submit('honest-3090', 'person@example.com')[0], 400)

    def test_oversized_notes_are_rejected_before_contact_regex(self):
        notes = 'a.' * (demo.MAX_NOTES_LENGTH // 2 + 1)
        self.assertGreater(len(notes), demo.MAX_NOTES_LENGTH)
        with patch('demo._notes_contain_contact', side_effect=AssertionError('contact regex must not run')) as detector:
            status, _, body = self.submit('honest-3090', notes)
        self.assertEqual(status, 400)
        self.assertIn(b'Notes cannot exceed 500 characters', body)
        detector.assert_not_called()

    def test_notify_email_never_leaks(self):
        marker = 'unique-private-marker@example.invalid'
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status, _, body = self.request('POST', '/notify', urlencode({'email': marker}), {'Content-Type': 'application/x-www-form-urlencoded'})
        self.assertEqual(status, 200)
        self.assertNotIn(marker.encode(), body)
        self.assertNotIn(marker, repr(demo.STORE))
        self.assertNotIn(marker, stdout.getvalue())
        self.assertNotIn(marker, stderr.getvalue())
        self.assertEqual(self.request('POST', '/notify', urlencode({'email': 'real@example.com'}), {'Content-Type': 'application/x-www-form-urlencoded'})[0], 400)

    def test_bad_requests_and_generic_exception(self):
        self.assertEqual(self.request('POST', '/check', b'')[0], 415)
        self.assertEqual(self.request('POST', '/check', b'', {'Content-Type': 'application/x-www-form-urlencoded', 'Content-Length': '0'})[0], 400)
        self.assertEqual(self.request('POST', '/check', urlencode({'scenario': 'not-real'}), {'Content-Type': 'application/x-www-form-urlencoded'})[0], 400)
        stderr = io.StringIO()
        with patch('demo.landing', side_effect=RuntimeError('private-marker')), contextlib.redirect_stderr(stderr):
            status, _, body = self.request('GET', '/')
        self.assertEqual(status, 500)
        self.assertNotIn(b'private-marker', body)
        self.assertNotIn('private-marker', stderr.getvalue())

    def test_no_outbound_connections(self):
        real_connect = socket.socket.connect
        def allow_loopback_only(sock, address):
            if address[0] not in {'127.0.0.1', '::1', 'localhost'}:
                raise AssertionError('outbound blocked')
            return real_connect(sock, address)
        with patch('socket.socket.connect', new=allow_loopback_only):
            status, _, _ = self.submit('honest-3090')
        self.assertEqual(status, 303)


if __name__ == '__main__':
    unittest.main()
