import html
import json
import os
import re
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

from cardcheck.baseline import BaselineError, parse_baseline
from cardcheck.compare import compare
from cardcheck.listing import ListingError, parse_listing
from cardcheck.models import CardIdentity, Record
from cardcheck.privacy import contains_contact
from cardcheck.record import build_record, canonical, record_html, record_json
from cardcheck.reference import REFERENCE
from cardcheck.smi import MAX_BYTES as MAX_FIELD, SmiError, detect, decode_text
from cardcheck.telemetry import FakeTelemetry, SCENARIOS
from cardcheck.workspace import Store, WorkspaceError, WorkspaceNotFound
from demo import BASELINES, CLAIMS, SilentThreadingHTTPServer

MAX_BODY = 1024 * 1024
FORM_FIELDS = frozenset(('label', 'model', 'vram_gb', 'board_partner', 'serial', 'vbios_version', 'notes', 'device_text', 'device_file', 'baseline_text', 'baseline_file'))
MAX_PARTS = len(FORM_FIELDS)
NOTICE = ("Operated by AI agents. Early version: keep your own copy of anything important. "
          "Do not enter other people's personal data (seller or buyer names, addresses, emails, phone numbers, order numbers). Nothing is charged.")
FIXED_RULES = 'Results come from fixed rules and a small reference table, not from AI.'
RETENTION = 'Workspaces expire after 90 days without viewing, exporting or saving. Empty workspaces expire 24 hours after creation. Keep your own copy of anything important.'
TOKEN_RE = r'[A-Za-z0-9_-]{32}'
CHECK_RE = r'[A-Za-z0-9_-]{12}'


def esc(value):
    return html.escape(str(value), quote=True)


def page(title, body, home='/'):
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
            f'<title>{esc(title)}</title><link rel="stylesheet" href="/static/style.css"><script src="/static/app.js" defer></script></head>'
            f'<body><div class="banner">{esc(NOTICE)} <a href="https://manykiln.com/privacy">Privacy</a></div>'
            f'<nav><a href="{esc(home)}">slotwise / CardCheck</a></nav>{body}<footer><p>{esc(FIXED_RULES)}</p></footer></body></html>').encode()


def _options(selected='RTX 3090'):
    return ''.join(f'<option value="{esc(name)}"{" selected" if name == selected else ""}>{esc(name)}</option>'
                   for name in sorted(REFERENCE) + ['Other (not in table)'])


def check_form(token, values=None, error=None):
    values = values or {}
    get = lambda key, default='': values.get(key, default)
    message = f'<div class="error"><strong>Could not save check:</strong> {esc(error)}</div>' if error else ''
    return (message + f'<form method="post" enctype="multipart/form-data" action="/w/{esc(token)}/check">'
            f'<label>Optional label<input name="label" maxlength="60" value="{esc(get("label"))}"></label>'
            f'<label>Claimed model<select name="model">{_options(get("model", "RTX 3090"))}</select></label>'
            f'<label>Claimed VRAM (GB)<input name="vram_gb" type="number" min="1" max="256" required value="{esc(get("vram_gb", "24"))}"></label>'
            f'<label>Board partner<input name="board_partner" maxlength="64" required value="{esc(get("board_partner", "NVIDIA"))}"></label>'
            f'<label>Listing serial (optional)<input name="serial" maxlength="64" value="{esc(get("serial"))}"></label>'
            f'<label>Listing BIOS version (optional)<input name="vbios_version" maxlength="64" value="{esc(get("vbios_version"))}"></label>'
            f'<label>Notes (optional; no personal/contact data)<textarea name="notes" maxlength="500">{esc(get("notes"))}</textarea></label>'
            '<p>Run this on the PC with the card installed. It reads values only and puts no load on the card.</p>'
            '<p>Serial numbers are often N/A on GeForce cards; that shows as not checkable, not as a pass.</p>'
            f'<label>Device data<textarea id="device_text" name="device_text" rows="12">{esc(get("device_text"))}</textarea></label>'
            '<label>Or upload device data (maximum 256 KB)<input type="file" data-fill="device_text" name="device_file"></label>'
            '<p>A seller file proves nothing by itself; matches mean consistent with the file.</p>'
            f'<label>Seller baseline (optional)<textarea id="baseline_text" name="baseline_text" rows="7">{esc(get("baseline_text"))}</textarea></label>'
            '<label>Or upload seller baseline (maximum 256 KB)<input type="file" data-fill="baseline_text" name="baseline_file"></label>'
            '<button>Create private check</button></form>')


def landing(message=''):
    command = ('nvidia-smi --query-gpu=name,pci.device_id,pci.sub_device_id,memory.total,vbios_version,serial,driver_version,'
               'pcie.link.gen.max,pcie.link.width.max,pcie.link.gen.current,pcie.link.width.current,temperature.gpu,power.draw,'
               'fan.speed,clocks.sm,clocks.mem,clocks_event_reasons.active --format=csv -l 1')
    return page('slotwise', f'<h1>slotwise CardCheck</h1><p class="success">{esc(message)}</p>'
                '<p>Compare a used NVIDIA card with listing claims, an optional unverified seller file, and a small local reference table. The hosted app never touches hardware or calls the network.</p>'
                '<p>This is read-only and runs no load test. It cannot detect physical fraud, prove condition at shipment, or promise marketplace acceptance.</p>'
                f'<p>{esc(RETENTION)}</p><form method="post" action="/start"><button>Start a workspace</button></form>'
                '<form method="post" action="/sample"><button>Try with sample data</button></form><h2>Collect device data</h2>'
                '<pre id="qcmd">nvidia-smi -q</pre><button type="button" data-copy="qcmd">Copy</button>'
                f'<pre id="csvcmd">{esc(command)}</pre><button type="button" data-copy="csvcmd">Copy</button>'
                '<p>Run the CSV command for about 60 seconds, then stop it with Ctrl+C.</p>')


def _baseline_from_record(record):
    identity = record['identity']
    data = {'schema': 'cardcheck.baseline/1', 'pci_device_id': identity['pci_device_id'], 'vram_total_mb': identity['vram_total_mb']}
    for key in ('vbios_version', 'serial'):
        if identity.get(key) is not None:
            data[key] = identity[key]
    data['captured_utc'] = record['created_utc']
    temps = [s['core_temp_c'] for s in record['samples'] if s.get('core_temp_c') is not None]
    if temps:
        data['idle_core_c'] = round(sum(temps) / len(temps), 1)
    return data


def _parse_multipart(body, content_type):
    match = re.search(r'boundary=(?:"([^"]+)"|([^;]+))', content_type)
    if not match:
        raise ValueError('Multipart boundary is missing.')
    boundary = (match.group(1) or match.group(2)).strip().encode('ascii', 'strict')
    if not 1 <= len(boundary) <= 70 or b'\r' in boundary or b'\n' in boundary:
        raise ValueError('Multipart boundary is invalid.')
    marker = b'--' + boundary
    # Only delimiters at line boundaries separate parts; a boundary string in
    # an uploaded file is ordinary data.
    if not body.startswith(marker + b'\r\n'):
        raise ValueError('Multipart form is malformed.')
    chunks = body[len(marker) + 2:].split(b'\r\n' + marker)
    if chunks[-1] not in (b'--', b'--\r\n'):
        raise ValueError('Multipart form is incomplete.')
    parts = chunks[:-1]
    if len(parts) > MAX_PARTS:
        raise ValueError('The form has too many parts.')
    out = {}
    for index, chunk in enumerate(parts):
        if index:
            if not chunk.startswith(b'\r\n'):
                raise ValueError('Multipart form is malformed.')
            chunk = chunk[2:]
        head, separator, data = chunk.partition(b'\r\n\r\n')
        if not separator or len(head) > 8192:
            raise ValueError('Multipart form is malformed.')
        if len(data) > MAX_FIELD:
            raise ValueError('A pasted or uploaded field exceeds 256 KB.')
        disposition = next((line for line in head.decode('latin-1').split('\r\n')
                            if line.lower().startswith('content-disposition:')), '')
        name_match = re.search(r'(?:^|;)\s*name="([^"]+)"', disposition)
        if not name_match or name_match[1] not in FORM_FIELDS or name_match[1] in out:
            raise ValueError('Unknown or repeated form field.')
        out[name_match[1]] = decode_text(data)
    return out


def _device_from_text(text):
    text = text.lstrip('\ufeff')
    if text.lstrip().startswith('{'):
        try:
            obj = json.loads(text)
        except ValueError:
            obj = None
        if isinstance(obj, dict) and obj.get('schema') == 'cardcheck.baseline/1':
            baseline = parse_baseline(text)
            values = baseline.values
            if 'pci_device_id' not in values or 'vram_total_mb' not in values:
                raise SmiError('A device baseline needs PCI device ID and VRAM.')
            identity = CardIdentity('Seller baseline (unverified)', values['pci_device_id'], None, values['vram_total_mb'],
                                    values.get('vbios_version'), values.get('serial'), None, None, None)
            return identity, [], ['Device input is a seller-supplied baseline and is unverified.']
    return detect(text)


class Handler(BaseHTTPRequestHandler):
    server_version = 'CardCheck'
    timeout = 10

    def log_message(self, format, *args):
        pass

    def _send(self, status, content, content_type='text/html; charset=utf-8', extra=None):
        self.send_response(status)
        headers = {'Content-Type': content_type, 'Content-Length': str(len(content)),
                   'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
                   'X-Frame-Options': 'DENY', 'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'same-origin', 'Connection': 'close'}
        if urlparse(self.path).path.startswith('/w/'):
            headers['Cache-Control'] = 'no-store'
        headers.update(extra or {})
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        self.close_connection = True
        if self.command != 'HEAD':
            self.wfile.write(content)

    def _error(self, status, message=None):
        generic = message or self.responses.get(status, ('Request rejected',))[0]
        self._send(status, page(f'Error {status}', f'<h1>Request could not be completed</h1><p>{esc(generic)}</p>'))

    def send_error(self, code, message=None, explain=None):
        self._error(code)

    def _host_ok(self):
        supplied = self.headers.get('Host', '').split(':', 1)[0].strip('[]').casefold()
        if supplied != (self.server.public.hostname or '').casefold():
            self._error(421, 'Request host is not accepted.')
            return False
        return True

    def _post_ok(self):
        origin = self.headers.get('Origin')
        referer = self.headers.get('Referer')
        expected = f'{self.server.public.scheme}://{self.server.public.netloc}'
        if origin == 'null' or (origin is not None and origin != expected) or (origin is None and not (referer or '').startswith(expected + '/')):
            self._error(403, 'Same-origin form submission required.')
            return False
        return True

    def _body(self):
        if self.headers.get('Transfer-Encoding'):
            self._error(400)
            return None
        lengths = self.headers.get_all('Content-Length', [])
        if len(lengths) != 1 or not re.fullmatch(r'[0-9]{1,10}', lengths[0]):
            self._error(411)
            return None
        length = int(lengths[0])
        if length > MAX_BODY:
            self._error(413, 'Request body exceeds 1 MB.')
            return None
        data = self.rfile.read(length)
        if len(data) != length:
            self._error(400)
            return None
        return data

    def _form(self):
        body = self._body()
        if body is None:
            return None
        content_type = self.headers.get('Content-Type', '')
        try:
            if content_type.startswith('multipart/form-data'):
                return _parse_multipart(body, content_type)
            if content_type.split(';', 1)[0].strip().casefold() == 'application/x-www-form-urlencoded':
                parsed = parse_qs(body.decode(), keep_blank_values=True, max_num_fields=MAX_PARTS)
                if any(key not in FORM_FIELDS or len(values) != 1 for key, values in parsed.items()):
                    raise ValueError('Unknown or repeated form field.')
                form = {key: values[0] for key, values in parsed.items()}
                if any(len(value.encode('utf-8')) > MAX_FIELD for value in form.values()):
                    raise ValueError('Field exceeds 256 KB.')
                return form
        except (UnicodeError, ValueError):
            self._error(400, 'Invalid form data.')
            return None
        self._error(415, 'Expected a browser form submission.')
        return None

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        try:
            if self._host_ok():
                self._get()
        except (BrokenPipeError, ConnectionError):
            pass
        except WorkspaceError:
            self._error(404, 'Workspace or check not found.')
        except Exception:
            self._error(500, 'Unexpected server error.')

    def _get(self):
        path = urlparse(self.path).path
        if path == '/healthz':
            self._send(200, b'ok', 'text/plain; charset=utf-8')
            return
        if path == '/':
            self._send(200, landing('Workspace deleted.' if parse_qs(urlparse(self.path).query).get('deleted') else ''))
            return
        if path in {'/static/style.css', '/static/app.js'}:
            filename = path.rsplit('/', 1)[1]
            self._send(200, (self.server.static_dir / filename).read_bytes(), 'text/css; charset=utf-8' if filename.endswith('.css') else 'text/javascript; charset=utf-8')
            return
        match = re.fullmatch(rf'/w/({TOKEN_RE})(?:/c/({CHECK_RE})(\.json|\.html)?(?:/(baseline\.json))?|/(export))?', path)
        if not match:
            self._error(404)
            return
        token, check_id, extension, baseline_name, export_name = match.groups()
        if export_name:
            payload = canonical(self.server.store.export(token))
            self._send(200, payload, 'application/json', {'Content-Disposition': 'attachment; filename="cardcheck-workspace.json"'})
            return
        if check_id:
            envelope = self.server.store.get_check(token, check_id)
            data = envelope['record']
            record = Record(data, data['sha256'])
            if extension == '.json':
                self._send(200, record_json(record), 'application/json', {'Content-Disposition': 'attachment; filename="cardcheck-record.json"'})
                return
            if extension == '.html':
                self._send(200, record_html(record, NOTICE + ' ' + FIXED_RULES).encode(), extra={'Content-Disposition': 'attachment; filename="cardcheck-record.html"'})
                return
            if baseline_name:
                self._send(200, canonical(_baseline_from_record(data)), 'application/json', {'Content-Disposition': 'attachment; filename="cardcheck-baseline.json"'})
                return
            rows = ''.join('<tr class="' + esc(f['severity']) + '">' + ''.join(f'<td>{esc(f[key])}</td>' for key in ('rule_id', 'severity', 'title', 'expected', 'observed', 'explanation')) + '</tr>' for f in data['findings'])
            unavailable = ''.join(f'<li>{esc(f["rule_id"])}: {esc(f["title"])} — {esc(f["explanation"])}</li>' for f in data['findings'] if f['severity'] == 'not_checkable') or '<li>None.</li>'
            inputs = {key: data[key] for key in ('claims', 'identity', 'baseline', 'samples')}
            body = (f'<h1>{esc(envelope.get("label") or "Private check")}</h1><p>{esc(data["created_utc"])} · {esc(data["summary"])}</p>'
                    f'<p><a class="button" href="/w/{token}/c/{check_id}.json">Download canonical JSON</a> <a class="button" href="/w/{token}/c/{check_id}.html">Printable HTML</a> <a class="button" href="/w/{token}/c/{check_id}/baseline.json">Seller baseline</a></p>'
                    f'<table><thead><tr><th>Rule</th><th>Severity</th><th>Finding</th><th>Expected</th><th>Observed</th><th>Explanation</th></tr></thead><tbody>{rows}</tbody></table>'
                    f'<h2>What we could not check</h2><ul>{unavailable}</ul><h2>Inputs that affected the result</h2><pre>{esc(json.dumps(inputs, indent=2))}</pre>'
                    f'<h2>Parser notes</h2><ul>{"".join(f"<li>{esc(n)}</li>" for n in envelope.get("parser_notes", [])) or "<li>None.</li>"}</ul>'
                    f'<h2>Limitations</h2><p>{esc(data["limitations"])}</p><p>SHA-256 accidental-change digest: <code>{esc(data["sha256"])}</code></p>'
                    f'<form method="post" action="/w/{token}/c/{check_id}/delete"><button class="danger">Delete this check</button></form><p><a href="/w/{token}">Back to workspace</a></p>')
            self._send(200, page('Private check', body, f'/w/{token}'))
            return
        items = self.server.store.list_checks(token)
        listing = ''.join(f'<li><a href="/w/{token}/c/{cid}">{esc(value.get("label") or value["record"]["claims"]["model"])}</a> — {esc(value["record"]["created_utc"])} — {esc(value["record"]["summary"])}</li>' for cid, value in items) or '<li>No checks yet.</li>'
        body = (f'<h1>Private workspace</h1><div class="warning"><strong>Bookmark this page.</strong> This link is the only key; it cannot be recovered.</div><p>{esc(RETENTION)}</p>'
                f'<p><a class="button" href="/w/{token}/export">Export everything</a></p><h2>History</h2><ul>{listing}</ul><h2>New check</h2>{check_form(token)}'
                f'<h2>Delete workspace</h2><form method="post" action="/w/{token}/delete"><button class="danger">Delete this workspace now</button></form>')
        self._send(200, page('Private workspace', body, f'/w/{token}'))

    def do_POST(self):
        try:
            if self._host_ok() and self._post_ok():
                self._post()
        except (BrokenPipeError, ConnectionError):
            pass
        except WorkspaceNotFound:
            self._error(404, 'Workspace or check not found.')
        except WorkspaceError as exc:
            self._error(400, str(exc))
        except Exception:
            self._error(500, 'Unexpected server error.')

    def _redirect(self, location):
        self._send(303, b'', extra={'Location': self.server.base + location})

    def _post(self):
        path = urlparse(self.path).path
        if path in {'/start', '/sample'}:
            if self._body() is None:
                return
            token = None
            try:
                token = self.server.store.create()
                if path == '/sample':
                    for scenario in SCENARIOS:
                        source = FakeTelemetry(scenario)
                        model, vram, partner, serial, bios = CLAIMS[scenario]
                        claims = parse_listing({'model': model, 'vram_gb': str(vram), 'board_partner': partner, 'serial': serial, 'vbios_version': bios})
                        baseline = parse_baseline(json.dumps(BASELINES[scenario]))
                        samples = [source.sample() for _ in range(60)]
                        record = build_record(claims, source.identity(), samples, compare(claims, source.identity(), samples, baseline), baseline)
                        self.server.store.put_check(token, {'record': record.data, 'label': scenario.replace('-', ' ').title(), 'parser_notes': ['Made-up sample data.'], 'sample': True})
            except WorkspaceError as exc:
                if token is not None:
                    self.server.store.delete(token)
                self._error(503, str(exc))
                return
            self._redirect('/w/' + token)
            return
        delete_workspace = re.fullmatch(rf'/w/({TOKEN_RE})/delete', path)
        delete_check = re.fullmatch(rf'/w/({TOKEN_RE})/c/({CHECK_RE})/delete', path)
        check = re.fullmatch(rf'/w/({TOKEN_RE})/check', path)
        if delete_workspace:
            if self._body() is not None:
                self.server.store.delete(delete_workspace[1])
                self._redirect('/?deleted=1')
            return
        if delete_check:
            if self._body() is not None:
                token, cid = delete_check.groups()
                self.server.store.delete_check(token, cid)
                self._redirect('/w/' + token)
            return
        if not check:
            self._error(404)
            return
        token = check[1]
        self.server.store.path(token)
        form = self._form()
        if form is None:
            return
        values = {key: form.get(key, '') for key in ('label', 'model', 'vram_gb', 'board_partner', 'serial', 'vbios_version', 'notes', 'device_text', 'baseline_text')}
        try:
            # Bound raw notes before any contact regex, including whitespace.
            if len(values['notes']) > 500:
                raise ValueError('Notes cannot exceed 500 characters.')
            label = values['label'].strip()
            if len(label) > 60 or contains_contact(label):
                raise ValueError('Label is too long or contains contact information.')
            claims = parse_listing(values, allow_other=True)
            if contains_contact(claims.notes):
                raise ValueError('Notes cannot contain email addresses or phone-like numbers.')
            device_text = (form.get('device_file') or values['device_text']).lstrip('\ufeff').strip()
            if not device_text:
                raise SmiError('Paste or upload device data.')
            identity, samples, notes = _device_from_text(device_text)
            baseline_text = (form.get('baseline_file') or values['baseline_text']).lstrip('\ufeff').strip()
            baseline = parse_baseline(baseline_text) if baseline_text else None
            record = build_record(claims, identity, samples, compare(claims, identity, samples, baseline), baseline)
            cid = self.server.store.put_check(token, {'record': record.data, 'label': label, 'parser_notes': notes, 'sample': False})
        except WorkspaceNotFound:
            raise
        except (ListingError, BaselineError, SmiError, WorkspaceError, ValueError) as exc:
            body = f'<h1>Private workspace</h1><p><a href="/w/{token}">Back to history</a></p><h2>New check</h2>{check_form(token, values, str(exc))}'
            self._send(400, page('Check error', body, f'/w/{token}'))
            return
        self._redirect(f'/w/{token}/c/{cid}')


class AppServer(SilentThreadingHTTPServer):
    def __init__(self, address, public_base_url, data_dir, cap_mb=200, **kwargs):
        public = urlparse(public_base_url)
        if public.scheme not in {'http', 'https'} or not public.hostname:
            raise ValueError('PUBLIC_BASE_URL must be an absolute HTTP(S) URL.')
        self.public = public
        self.base = f'{public.scheme}://{public.netloc}'
        self.store = Store(data_dir, cap_mb)
        self.store.sweep()
        from pathlib import Path
        self.static_dir = Path(__file__).resolve().parent / 'static'
        super().__init__(address, Handler, max_connections=32, request_deadline=10, **kwargs)
        self.store.start_sweeper()

    def server_close(self):
        self.store.close()
        super().server_close()


def make_server(host=None, port=None, public_base_url=None, data_dir=None, cap_mb=None):
    host = host if host is not None else os.environ.get('APP_HOST', '127.0.0.1')
    port = int(port if port is not None else os.environ.get('PORT', '8080'))
    public_base_url = public_base_url or os.environ.get('PUBLIC_BASE_URL', 'https://app-slotwise.manykiln.com')
    data_dir = data_dir or os.environ.get('DATA_DIR')
    if not data_dir:
        raise ValueError('DATA_DIR is required.')
    cap_mb = int(cap_mb if cap_mb is not None else os.environ.get('APP_DATA_CAP_MB', '200'))
    return AppServer((host, port), public_base_url, data_dir, cap_mb)


def main():
    server = make_server()
    try:
        server.serve_forever(poll_interval=0.1)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
