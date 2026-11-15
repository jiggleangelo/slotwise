import html
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import urlencode, urlparse

import app
from cardcheck.record import validate_digest

Q_TEXT = '''==============NVSMI LOG==============
Driver Version                            : 555.1
Attached GPUs                             : 1
GPU 00000000:01:00.0
    Product Name                          : NVIDIA GeForce RTX 3090
    Serial Number                         : N/A
    VBIOS Version                         : 94.02.42.00.01
    PCI
        Device Id                         : 0x220410DE
        Sub System Id                     : 0x30903842
        PCIe Generation
            Max                           : 4
            Current                       : 1
        Link Width
            Max                           : 16x
            Current                       : 16x
    FB Memory Usage
        Total                             : 24576 MiB
    Temperature
        GPU Current Temp                  : 42 C
        GPU Hotspot Temp                  : 51 C
    Power Readings
        Power Draw                        : 32 W
    Fan Speed                             : 30 %
    Clocks
        Graphics                          : 210 MHz
        Memory                            : 405 MHz
    Clocks Event Reasons
        Idle                              : Active
'''


class AppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = app.make_server('127.0.0.1', 0, 'http://127.0.0.1:1', self.temp.name)
        self.port = self.server.server_port
        self.server.public = urlparse(f'http://127.0.0.1:{self.port}')
        self.server.base = f'http://127.0.0.1:{self.port}'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.temp.cleanup()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=3)
        origin = f'http://127.0.0.1:{self.port}'
        supplied = {'Host': f'127.0.0.1:{self.port}'}
        if method == 'POST':
            supplied.update({'Origin': origin, 'Referer': origin + path, 'Content-Type': 'application/x-www-form-urlencoded'})
        supplied.update(headers or {})
        try:
            conn.request(method, path, body, supplied)
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def start(self):
        status, headers, _ = self.request('POST', '/start', b'')
        self.assertEqual(status, 303)
        return urlparse(headers['Location']).path

    def test_health_core_export_delete_and_no_token_on_disk(self):
        status, _, body = self.request('GET', '/healthz')
        self.assertEqual((status, body), (200, b'ok'))
        status, headers, body = self.request('GET', '/')
        self.assertEqual(status, 200)
        # Compare rendered text rather than its HTML serialization: page() safely
        # escapes the apostrophe in "people's" as &#x27;.
        self.assertIn(app.NOTICE, html.unescape(body.decode('utf-8')))
        self.assertIn(b'https://manykiln.com/privacy', body)
        self.assertEqual(headers['Referrer-Policy'], 'same-origin')
        workspace = self.start()
        token = workspace.rsplit('/', 1)[1]
        baseline = json.dumps({'schema': 'cardcheck.baseline/1', 'serial': 'DIFFERENT'})
        payload = urlencode({'model': 'RTX 3090', 'vram_gb': '24', 'board_partner': 'EVGA', 'serial': 'LISTING',
                             'notes': '<script>escaped</script>', 'device_text': Q_TEXT, 'baseline_text': baseline})
        status, headers, _ = self.request('POST', workspace + '/check', payload)
        self.assertEqual(status, 303)
        check = urlparse(headers['Location']).path
        status, _, body = self.request('GET', check)
        self.assertEqual(status, 200)
        self.assertIn(b'R10', body)
        self.assertIn(b'R9', body)
        self.assertIn(b'&lt;script&gt;', body)
        status, _, raw = self.request('GET', check + '.json')
        self.assertEqual(status, 200)
        self.assertTrue(validate_digest(json.loads(raw)))
        self.assertEqual(self.request('GET', check + '.html')[0], 200)
        self.assertEqual(self.request('GET', check + '/baseline.json')[0], 200)
        status, _, raw = self.request('GET', workspace + '/export')
        exported = json.loads(raw)
        self.assertEqual(status, 200)
        self.assertEqual(len(exported['checks']), 1)
        self.assertTrue(validate_digest(exported['checks'][0]))
        disk = Path(self.temp.name)
        self.assertNotIn(token, '\n'.join(str(path) for path in disk.rglob('*')))
        self.assertNotIn(token, b'\n'.join(path.read_bytes() for path in disk.rglob('*') if path.is_file()).decode('utf-8', 'ignore'))
        self.assertNotIn(Q_TEXT, b'\n'.join(path.read_bytes() for path in disk.rglob('*') if path.is_file()).decode('utf-8', 'ignore'))
        self.assertEqual(self.request('POST', workspace + '/delete', b'')[0], 303)
        self.assertEqual(self.request('GET', workspace)[0], 404)

    def test_host_origin_privacy_and_body_limits(self):
        self.assertEqual(self.request('GET', '/', headers={'Host': 'evil.invalid'})[0], 421)
        self.assertEqual(self.request('POST', '/start', b'', {'Origin': 'https://evil.invalid'})[0], 403)
        self.assertEqual(self.request('POST', '/start', b'', {'Origin': 'null'})[0], 403)
        workspace = self.start()
        payload = urlencode({'model': 'RTX 3090', 'vram_gb': '24', 'board_partner': 'EVGA', 'notes': 'person@example.com', 'device_text': Q_TEXT})
        self.assertEqual(self.request('POST', workspace + '/check', payload)[0], 400)
        self.assertEqual(self.request('POST', '/start', b'x' * (1024 * 1024 + 1))[0], 413)


if __name__ == '__main__':
    unittest.main()
