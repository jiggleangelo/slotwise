import html
import json
import os
import re
import secrets
import socket
import threading
import time
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from cardcheck.baseline import BaselineError, parse_baseline
from cardcheck.compare import compare
from cardcheck.listing import ListingError, parse_listing
from cardcheck.observe import observe
from cardcheck.payments import FakePayments
from cardcheck.record import build_record, record_html, record_json
from cardcheck.telemetry import FakeTelemetry, SCENARIOS, TelemetryUnavailable
from cardcheck.vault import FakeVault

BANNER = 'Demo operated by AI agents. Made-up data. Do not enter real personal data. Nothing is charged.'
STORE = OrderedDict()
STORE_LOCK = threading.Lock()
# Compatibility for existing callers; no addresses or rate events are retained.
RATE_EVENTS = {}
VAULT = FakeVault()
PAYMENTS = FakePayments()
MAX_BODY = 65536
MAX_NOTES_LENGTH = 500
MAX_RECORDS = 1000
MAX_CONNECTIONS = 32
REQUEST_DEADLINE_SECONDS = 15
RECORD_TTL_SECONDS = 30 * 60
STATIC_DIR = Path(__file__).resolve().parent / 'static'
TOKEN_RE = re.compile(r'[A-Za-z0-9_-]{32}')
RECORD_PATH_RE = re.compile(r'/record/([A-Za-z0-9_-]{32})(\.json|\.html)?')
EMAIL_RE = re.compile(r'\b[A-Z0-9._%+-]{1,64}@[A-Z0-9.-]+\.[A-Z]{2,63}\b', re.I)
PHONE_RE = re.compile(r'(?<!\w)(?:\+?\d[\s().-]*){7,15}(?!\w)')
DEMO_EMAIL_RE = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@example\.invalid", re.I)
BASELINES = {
    'honest-3090': {'schema': 'cardcheck.baseline/1', 'pci_device_id': '0x2204', 'vram_total_mb': 24576, 'vbios_version': '94.02.42.00.01', 'serial': 'DEMO-0001'},
    'relabelled-4090': {'schema': 'cardcheck.baseline/1', 'pci_device_id': '0x2684', 'vram_total_mb': 24576, 'serial': 'DEMO-4090'},
    'worn-3090': {'schema': 'cardcheck.baseline/1', 'pci_device_id': '0x2204', 'vram_total_mb': 24576, 'serial': 'DEMO-0001'},
}
CLAIMS = {
    'honest-3090': ('RTX 3090', 24, 'EVGA', 'DEMO-0001', '94.02.42.00.01'),
    'relabelled-4090': ('RTX 4090', 24, 'NVIDIA', 'DEMO-4090', 'DEMO.BIOS'),
    'worn-3090': ('RTX 3090', 24, 'EVGA', 'DEMO-9999', '94.02.42.00.01'),
}


def esc(value):
    return html.escape(str(value), quote=True)


def page(title, body):
    return (f'<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content="width=device-width">'
            f'<title>{esc(title)}</title><link rel=stylesheet href=/static/style.css>'
            f'<script src=/static/app.js defer></script></head><body><div class=banner>{esc(BANNER)}</div>'
            f'<nav><a href=/>Check</a> · <a href=/pricing>Pricing test</a></nav>{body}</body></html>').encode('utf-8')


def landing(scenario):
    scenario = scenario if scenario in CLAIMS else 'honest-3090'
    model, vram, partner, serial, bios = CLAIMS[scenario]
    cards = ''.join(f'<div class=card><h2>{esc(key.replace("-", " ").title())}</h2><a class=button href="/?scenario={esc(key)}">Use scenario</a></div>' for key in SCENARIOS)
    options = ''.join(f'<option{" selected" if name == model else ""}>{esc(name)}</option>' for name in sorted({claim[0] for claim in CLAIMS.values()}))
    fields = ''.join(f'<label>{label}<input name={name} value="{esc(value)}"></label>' for label, name, value in
                     [('VRAM (GB)', 'vram_gb', vram), ('Board partner', 'board_partner', partner), ('Serial (optional)', 'serial', serial), ('BIOS version (optional)', 'vbios_version', bios)])
    return page('CardCheck demo',
                f'<h1>CardCheck</h1><p>A private, read-only delivery check. It creates no GPU load and cannot prove shipment condition or detect physical fraud.</p>'
                f'<p>Marketplace acceptance is unproven. Even read-only tools may affect marketplace alteration rules; check those rules before use.</p>'
                f'<div class=cards>{cards}</div><h2>Run the selected check</h2><form method=post action=/check>'
                f'<input type=hidden name=scenario value="{esc(scenario)}"><label>Claimed model<select name=model>{options}</select></label>{fields}'
                f'<label>Notes — email addresses and phone-like numbers are rejected<textarea name=notes maxlength={MAX_NOTES_LENGTH}></textarea></label>'
                f'<label>Seller baseline (seller-supplied, unverified; optional)<textarea name=baseline rows=8>{esc(json.dumps(BASELINES[scenario], indent=2))}</textarea></label>'
                f'<p>The demo simulates 60 samples without waiting. Real observations sample at 1 Hz.</p><button>Run simulated idle check</button></form>')


def _first(form, key):
    return form.get(key, [''])[0]


def _purge_records(now=None):
    now = time.monotonic() if now is None else now
    for token in [token for token, entry in STORE.items() if entry[0] <= now]:
        STORE.pop(token, None)


def _put_record(record):
    token = secrets.token_urlsafe(24)
    with STORE_LOCK:
        _purge_records()
        STORE[token] = (time.monotonic() + RECORD_TTL_SECONDS, record)
        while len(STORE) > MAX_RECORDS:
            STORE.popitem(last=False)
    return token


def _get_record(token):
    with STORE_LOCK:
        _purge_records()
        entry = STORE.get(token)
        return entry[1] if entry else None


def _notes_contain_contact(value):
    return bool(EMAIL_RE.search(value) or PHONE_RE.search(value))


class Handler(BaseHTTPRequestHandler):
    server_version = 'CardCheckDemo'
    timeout = 10

    def log_message(self, format, *args):
        pass

    def _send(self, status, content, content_type='text/html; charset=utf-8', extra=None):
        self.send_response(status)
        for key, value in {'Content-Type': content_type, 'Content-Length': str(len(content)),
                           'Content-Security-Policy': "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
                           'X-Frame-Options': 'DENY', 'X-Content-Type-Options': 'nosniff',
                           'Referrer-Policy': 'no-referrer', 'Cache-Control': 'no-store',
                           'Connection': 'close', **(extra or {})}.items():
            self.send_header(key, value)
        self.end_headers()
        self.close_connection = True
        if getattr(self, 'command', None) != 'HEAD':
            self.wfile.write(content)

    def _error(self, status, message):
        self._send(status, page(f'Error {status}', f'<h1>Error</h1><p>{esc(message)}</p>'))

    def send_error(self, code, message=None, explain=None):
        # Includes parser errors and unsupported methods; never echo request text.
        self._error(code, self.responses.get(code, ('Request rejected',))[0])

    def do_HEAD(self):
        self.do_GET()

    def _body(self):
        if self.headers.get('Transfer-Encoding'):
            self._error(400, 'Transfer-Encoding is unsupported.')
            return None
        lengths = self.headers.get_all('Content-Length', [])
        if not lengths:
            self._error(411, 'Content-Length is required.')
            return None
        if len(lengths) != 1 or not re.fullmatch(r'[0-9]{1,10}', lengths[0]):
            self._error(400, 'Invalid Content-Length.')
            return None
        length = int(lengths[0])
        if length > MAX_BODY:
            self._error(413, 'Request body exceeds 64 KB.')
            return None
        try:
            body = self.rfile.read(length)
        except (OSError, TimeoutError):
            self._error(408, 'Request body timed out.')
            return None
        if len(body) != length:
            self._error(400, 'Incomplete request body.')
            return None
        return body

    def _form(self):
        if self.headers.get('Content-Type', '').split(';', 1)[0].strip().lower() != 'application/x-www-form-urlencoded':
            self._error(415, 'Expected a form submission.')
            return None
        body = self._body()
        if body is None:
            return None
        try:
            return parse_qs(body.decode('utf-8'), keep_blank_values=True, max_num_fields=32)
        except (UnicodeDecodeError, ValueError):
            self._error(400, 'Invalid form data.')
            return None

    def _dispatch(self, operation):
        try:
            operation()
        except (BrokenPipeError, ConnectionError):
            pass
        except Exception:
            try:
                self._error(500, 'Unexpected server error.')
            except Exception:
                pass

    def do_GET(self):
        self._dispatch(self._do_GET)

    def _do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == '/healthz':
            self._send(200, b'ok', 'text/plain; charset=utf-8')
        elif parsed.path == '/':
            self._send(200, landing(parse_qs(parsed.query, max_num_fields=32).get('scenario', ['honest-3090'])[0]))
        elif parsed.path in {'/static/style.css', '/static/app.js'}:
            filename = parsed.path.rsplit('/', 1)[1]
            self._send(200, (STATIC_DIR / filename).read_bytes(), 'text/css; charset=utf-8' if filename.endswith('.css') else 'text/javascript; charset=utf-8')
        elif parsed.path == '/pricing':
            clicked = bool(parse_qs(parsed.query, max_num_fields=32).get('clicked'))
            message = '<p><strong>Demo checkout selected. Nothing is charged.</strong></p>' if clicked else ''
            links = ''.join(f'<p><a class=button href="{esc(PAYMENTS.checkout_url(sku))}">Fake checkout — {price}</a></p>' for sku, price in [('single', '$9'), ('five', '$29 for 5')])
            self._send(200, page('Pricing', f'<h1>Pricing intent test</h1>{message}{links}<form method=post action=/notify>'
                                '<label>Demo-only email (must end in @example.invalid)<input type=email name=email required></label><button>Save to write-only demo vault</button></form>'))
        else:
            match = RECORD_PATH_RE.fullmatch(parsed.path)
            record = _get_record(match[1]) if match else None
            if record is None:
                self._error(404, 'Record not found or expired.' if match else 'Page not found.')
                return
            token, extension = match.groups()
            if extension == '.json':
                self._send(200, record_json(record), 'application/json; charset=utf-8', {'Content-Disposition': f'attachment; filename="cardcheck-{token}.json"'})
            elif extension == '.html':
                self._send(200, record_html(record, BANNER).encode('utf-8'), extra={'Content-Disposition': f'attachment; filename="cardcheck-{token}.html"'})
            else:
                rows = ''.join('<tr class="' + esc(item['severity']) + '">' + ''.join(f'<td>{esc(item[key])}</td>' for key in ('rule_id', 'severity', 'title', 'expected', 'observed', 'explanation')) + '</tr>' for item in record.data['findings'])
                self._send(200, page('CardCheck record', f'<h1>Private check record</h1><p>{esc(record.data["summary"])}</p>'
                                    f'<p>Created: {esc(record.data["created_utc"])}</p><p><strong>Notes:</strong> {esc(record.data["claims"].get("notes") or "None")}</p>'
                                    f'<p><a class=button href="/record/{token}.json">Download JSON</a> <a class=button href="/record/{token}.html">Download printable HTML</a></p>'
                                    f'<table><tr><th>Rule</th><th>Severity</th><th>Finding</th><th>Expected</th><th>Observed</th><th>Explanation</th></tr>{rows}</table>'
                                    f'<h2>Limitations</h2><p>{esc(record.data["limitations"])}</p><p>SHA-256: <code>{esc(record.sha256)}</code></p>'))

    def do_POST(self):
        self._dispatch(self._do_POST)

    def _do_POST(self):
        path = urlparse(self.path).path
        if path not in {'/check', '/notify'}:
            self._error(404, 'Page not found.')
            return
        form = self._form()
        if form is None:
            return
        if path == '/notify':
            email = _first(form, 'email').strip()
            if len(email) > 254 or not DEMO_EMAIL_RE.fullmatch(email):
                self._error(400, 'Use a demo-only address ending in @example.invalid.')
                return
            try:
                VAULT.store('email', email)
            except ValueError:
                self._error(400, 'Unable to save the demo address.')
                return
            self._send(200, page('Saved', '<h1>Saved</h1><p>The demo vault accepted the address without making it readable to the app.</p>'))
            return
        scenario, notes = _first(form, 'scenario'), _first(form, 'notes')
        if scenario not in SCENARIOS:
            self._error(400, 'Choose a valid demo scenario.')
            return
        if len(notes) > MAX_NOTES_LENGTH:
            self._error(400, f'Notes cannot exceed {MAX_NOTES_LENGTH} characters.')
            return
        if _notes_contain_contact(notes):
            self._error(400, 'Notes cannot contain email addresses or phone-like numbers.')
            return
        try:
            claims = parse_listing({key: _first(form, key) for key in ('model', 'vram_gb', 'board_partner', 'serial', 'vbios_version', 'notes')})
            baseline_text = _first(form, 'baseline').strip()
            baseline = parse_baseline(baseline_text) if baseline_text else None
            source = FakeTelemetry(scenario)
            identity = source.identity()
            samples = observe(source, 60, lambda _: None)
            record = build_record(claims, identity, samples, compare(claims, identity, samples, baseline), baseline)
        except (ListingError, BaselineError, TelemetryUnavailable, ValueError) as exc:
            self._error(400, str(exc))
            return
        self._send(303, b'', extra={'Location': '/record/' + _put_record(record)})


class SilentThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, address, handler, max_connections=MAX_CONNECTIONS, request_deadline=REQUEST_DEADLINE_SECONDS):
        self._slots = threading.BoundedSemaphore(max_connections)
        self._active = {}
        self._active_lock = threading.Lock()
        self.request_deadline = request_deadline
        super().__init__(address, handler)

    @staticmethod
    def _disconnect(request):
        try:
            request.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        request.close()

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        with self._active_lock:
            self._active[request] = time.monotonic() + self.request_deadline
        try:
            super().process_request(request, client_address)
        except BaseException:
            with self._active_lock:
                self._active.pop(request, None)
            self._slots.release()
            self.shutdown_request(request)
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self._active_lock:
                self._active.pop(request, None)
            self._slots.release()

    def service_actions(self):
        # The accept loop enforces wall-clock deadlines even for drip-fed bytes.
        with self._active_lock:
            expired = [request for request, deadline in self._active.items() if deadline <= time.monotonic()]
        for request in expired:
            self._disconnect(request)

    def server_close(self):
        with self._active_lock:
            requests = list(self._active)
        for request in requests:
            self._disconnect(request)
        super().server_close()

    def handle_error(self, request, client_address):
        pass


def make_server(host='0.0.0.0', port=8080, **kwargs):
    return SilentThreadingHTTPServer((host, port), Handler, **kwargs)


def main():
    server = make_server('0.0.0.0', int(os.environ.get('PORT', '8080')))
    try:
        server.serve_forever(poll_interval=0.1)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
