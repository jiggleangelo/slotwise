import hashlib
import html
import json
from dataclasses import asdict
from datetime import datetime, timezone
from .version import __version__
from .compare import LIMITATIONS, summary
from .models import Record


def canonical(data):
    return json.dumps(data, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')


def build_record(claims, identity, samples, findings, baseline=None, created_utc=None):
    data = {
        'schema': 'cardcheck.record/1', 'tool_version': __version__,
        'created_utc': created_utc or datetime.now(timezone.utc).isoformat(),
        'clock_note': "Uses this PC's clock; not independently timestamped.",
        'claims': asdict(claims), 'identity': asdict(identity),
        'samples': [asdict(item) for item in samples],
        'baseline': ({**baseline.values, 'source': baseline.source, 'ignored_keys': baseline.ignored_keys} if baseline else None),
        'findings': [asdict(item) for item in findings], 'summary': summary(findings), 'limitations': LIMITATIONS,
    }
    digest = hashlib.sha256(canonical(data)).hexdigest()
    data['sha256'] = digest
    data['digest_purpose'] = 'Detect accidental changes to this file; this is not a tamper-proof seal.'
    return Record(data, digest)


def validate_digest(data):
    copy = dict(data)
    digest = copy.pop('sha256', None)
    copy.pop('digest_purpose', None)
    return bool(digest) and hashlib.sha256(canonical(copy)).hexdigest() == digest


def record_json(record):
    return canonical(record.data)


def record_html(record, banner=None):
    esc = lambda value: html.escape(str(value), quote=True)
    data = record.data
    rows = ''.join(
        '<tr>' + ''.join(f'<td>{esc(f[key])}</td>' for key in ('rule_id', 'severity', 'title', 'expected', 'observed', 'explanation')) + '</tr>'
        for f in data['findings'])
    notice = f'<p>{esc(banner)}</p>' if banner else ''
    details = ''.join(f'<h2>{esc(key.title())}</h2><pre>{esc(json.dumps(data[key], ensure_ascii=False, indent=2))}</pre>'
                      for key in ('claims', 'identity', 'baseline', 'samples'))
    return (f'<!doctype html><html lang=en><head><meta charset=utf-8><title>CardCheck private record</title></head><body>{notice}'
            f'<h1>CardCheck private record</h1><p>Created: {esc(data["created_utc"])} · Tool: {esc(data["tool_version"])}</p>'
            f'<p>{esc(data["clock_note"])}</p><p>{esc(data["summary"])}</p>'
            f'<table border=1><thead><tr><th>Rule</th><th>Severity</th><th>Finding</th><th>Expected</th><th>Observed</th><th>Explanation</th></tr></thead><tbody>{rows}</tbody></table>'
            f'<h2>Limitations</h2><p>{esc(data["limitations"])}</p><p>SHA-256 of the canonical JSON payload: <code>{esc(record.sha256)}</code></p>'
            f'<p>{esc(data["digest_purpose"])}</p>{details}</body></html>')
