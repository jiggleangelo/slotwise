import hashlib
import json
import os
import re
import secrets
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

MAX_WORKSPACES = 3000
MAX_CHECKS = 100
MAX_WORKSPACE_BYTES = 5 * 1024 * 1024
NEW_PER_HOUR = 60
CHECKS_PER_HOUR = 30
IDLE_SECONDS = 90 * 86400
EMPTY_SECONDS = 86400
# Covers usage.json and small metadata growth, including read timestamps.
ACCOUNTING_RESERVE = 8192


class WorkspaceError(ValueError):
    pass


class WorkspaceNotFound(WorkspaceError):
    pass


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()


def _stamp():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


class Store:
    """One Store owns a data directory in the app's single server process.

    A stable transaction lock protects both disk mutations and the byte ledger.
    Reads of check files are also protected against deletion. No lock cache can
    evict an active lock. Disk reconciliation runs once at startup; ordinary
    operations inspect only the selected workspace (at most 100 checks).
    """

    def __init__(self, root, cap_mb=200):
        self.root = Path(root)
        self.ws = self.root / 'ws'
        self.tomb = self.root / 'tomb'
        self.root.mkdir(parents=True, exist_ok=True)
        self.ws.mkdir(exist_ok=True)
        self.tomb.mkdir(exist_ok=True)
        self.cap = int(cap_mb) * 1024 * 1024
        self._global = threading.RLock()
        self._stop = threading.Event()
        self._sweeper = None
        # Include unknown and abandoned temporary files conservatively.
        self._bytes = sum(p.stat().st_size for p in self.root.rglob('*')
                          if p.is_file() and p != self.root / 'usage.json')
        self._workspaces = sum(p.is_dir() for p in self.ws.iterdir())
        old = self._json(self.root / 'usage.json') or {}
        self._events = old.get('workspace_events', [])
        self._write_usage()

    @staticmethod
    def hash_token(token):
        if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{32}', token):
            raise WorkspaceNotFound('Workspace not found.')
        return hashlib.sha256(token.encode('ascii')).hexdigest()

    def _lock(self, digest):
        # Stable for the lifetime of the Store, including unknown tokens.
        return self._global

    def _atomic(self, path, data):
        old_size = path.stat().st_size if path.exists() else 0
        tmp = path.with_name('.' + path.name + '.' + secrets.token_hex(6) + '.tmp')
        descriptor = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, 'wb') as stream:
                descriptor = -1
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, path)
            if path != self.root / 'usage.json':
                self._bytes += len(data) - old_size
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass

    def _unlink(self, path):
        size = path.stat().st_size
        path.unlink()
        self._bytes -= size

    def _json(self, path):
        try:
            return json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return None

    def _size(self):
        return self._bytes + ACCOUNTING_RESERVE

    def _room(self, growth):
        if self._size() + growth > self.cap:
            raise WorkspaceError('The app data limit has been reached; new writes are temporarily refused.')

    def _write_usage(self, events=None):
        now = time.time()
        self._events = [x for x in self._events if isinstance(x, (int, float)) and x > now - 3600]
        self._events.extend(events or [])
        data = {'bytes': self._bytes, 'workspaces': self._workspaces, 'workspace_events': self._events}
        self._atomic(self.root / 'usage.json', _encoded(data))
        return data

    def create(self):
        with self._global:
            usage = self._write_usage()
            if self._workspaces >= MAX_WORKSPACES:
                raise WorkspaceError('The service has reached its workspace limit.')
            if len(usage['workspace_events']) >= NEW_PER_HOUR:
                raise WorkspaceError('The service has reached its hourly new-workspace limit. Try later.')
            now = _stamp()
            payload = _encoded({'created': now, 'last_used': now, 'bytes': 0, 'check_events': []})
            self._room(len(payload))
            while True:
                token = secrets.token_urlsafe(24)
                digest = self.hash_token(token)
                path = self.ws / digest
                if not path.exists() and not (self.tomb / digest).exists():
                    break
            path.mkdir(mode=0o700)
            try:
                (path / 'checks').mkdir(mode=0o700)
                self._atomic(path / 'meta.json', payload)
            except Exception:
                shutil.rmtree(path)
                raise
            self._workspaces += 1
            self._write_usage([time.time()])
            return token

    def path(self, token):
        with self._global:
            digest = self.hash_token(token)
            path = self.ws / digest
            if (self.tomb / digest).exists() or not (path / 'meta.json').is_file():
                raise WorkspaceNotFound('Workspace not found.')
            return digest, path

    def _meta(self, path):
        meta = self._json(path / 'meta.json')
        if not isinstance(meta, dict):
            raise WorkspaceNotFound('Workspace not found.')
        return meta

    def _touch(self, path, meta=None):
        meta = self._meta(path) if meta is None else meta
        meta['last_used'] = _stamp()
        self._atomic(path / 'meta.json', _encoded(meta))
        return meta

    def get_meta(self, token):
        with self._global:
            _, path = self.path(token)
            return self._touch(path)

    def _checks(self, path):
        result = []
        for item in (path / 'checks').glob('*.json'):
            value = self._json(item)
            if isinstance(value, dict) and isinstance(value.get('record'), dict):
                result.append((item.stem, value))
        return sorted(result, key=lambda pair: pair[1]['record'].get('created_utc', ''), reverse=True)

    def list_checks(self, token):
        with self._global:
            _, path = self.path(token)
            result = self._checks(path)
            self._touch(path)
            return result

    @staticmethod
    def _check_id(check_id):
        if not isinstance(check_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{12}', check_id):
            raise WorkspaceNotFound('Check not found.')

    def get_check(self, token, check_id):
        with self._global:
            self._check_id(check_id)
            _, path = self.path(token)
            value = self._json(path / 'checks' / (check_id + '.json'))
            if not isinstance(value, dict) or not isinstance(value.get('record'), dict):
                raise WorkspaceNotFound('Check not found.')
            self._touch(path)
            return value

    def export(self, token):
        with self._global:
            _, path = self.path(token)
            items = self._checks(path)
            meta = self._touch(path)
            # Preserve the existing record list and its digests. The parallel
            # IDs and metadata retain every envelope field without changing it.
            return {'schema': 'cardcheck.workspace/1', 'created': meta['created'],
                    'workspace': meta, 'checks': [value['record'] for _, value in items],
                    'check_ids': [cid for cid, _ in items],
                    'check_metadata': {cid: {key: value for key, value in envelope.items() if key != 'record'}
                                       for cid, envelope in items}}

    def put_check(self, token, envelope):
        payload = _encoded(envelope)
        if len(payload) > 512 * 1024:
            raise WorkspaceError('The resulting record is too large.')
        with self._global:
            _, path = self.path(token)
            meta = self._meta(path)
            checks = list((path / 'checks').glob('*.json'))
            if len(checks) >= MAX_CHECKS:
                raise WorkspaceError('This workspace already has 100 checks.')
            now = time.time()
            events = [x for x in meta.get('check_events', []) if isinstance(x, (int, float)) and x > now - 3600]
            if len(events) >= CHECKS_PER_HOUR:
                raise WorkspaceError('This workspace has reached its hourly check limit.')
            current = sum(item.stat().st_size for item in checks)
            if current + len(payload) > MAX_WORKSPACE_BYTES:
                raise WorkspaceError('This workspace has reached its 5 MB limit.')
            meta.update(last_used=_stamp(), bytes=current + len(payload), check_events=events + [now])
            metadata = _encoded(meta)
            self._room(len(payload) + len(metadata) - (path / 'meta.json').stat().st_size)
            while True:
                check_id = secrets.token_urlsafe(9)
                target = path / 'checks' / (check_id + '.json')
                if not target.exists():
                    break
            self._atomic(target, payload)
            try:
                self._atomic(path / 'meta.json', metadata)
            except Exception:
                self._unlink(target)
                raise
            self._write_usage()
            return check_id

    def delete_check(self, token, check_id):
        with self._global:
            self._check_id(check_id)
            _, path = self.path(token)
            target = path / 'checks' / (check_id + '.json')
            if not target.is_file():
                raise WorkspaceNotFound('Check not found.')
            self._unlink(target)
            meta = self._meta(path)
            meta['bytes'] = sum(p.stat().st_size for p in (path / 'checks').glob('*.json'))
            self._touch(path, meta)
            self._write_usage()

    def _delete_path(self, path):
        # Only called for an existing workspace, under the transaction lock.
        # Marker count is bounded by successful, rate-limited creations.
        self._atomic(self.tomb / path.name, b'deleted\n')
        for item in path.rglob('*'):
            if item.is_file():
                self._unlink(item)
        shutil.rmtree(path)
        self._workspaces -= 1

    def delete(self, token):
        with self._global:
            _, path = self.path(token)
            self._delete_path(path)
            self._write_usage()

    def sweep(self, now=None):
        now = time.time() if now is None else now
        # Snapshot names only. Re-read expiry under the same lock as touches
        # and writes so a concurrent visit cannot be deleted using stale data.
        with self._global:
            markers = list(self.tomb.iterdir())
            paths = list(self.ws.iterdir())
        for marker in markers:
            with self._global:
                if marker.exists() and marker.stat().st_mtime < now - 86400:
                    self._unlink(marker)
        for path in paths:
            with self._global:
                meta = self._json(path / 'meta.json')
                if not isinstance(meta, dict):
                    continue
                try:
                    created = datetime.fromisoformat(meta['created']).timestamp()
                    used = datetime.fromisoformat(meta['last_used']).timestamp()
                except (KeyError, TypeError, ValueError):
                    continue
                empty = not any((path / 'checks').glob('*.json'))
                if used < now - IDLE_SECONDS or (empty and created < now - EMPTY_SECONDS):
                    self._delete_path(path)
        with self._global:
            self._write_usage()

    def start_sweeper(self):
        def run():
            while not self._stop.wait(86400):
                try:
                    self.sweep()
                except Exception:
                    pass
        if self._sweeper is None:
            self._sweeper = threading.Thread(target=run, daemon=True, name='cardcheck-sweep')
            self._sweeper.start()
        return self._sweeper

    def close(self):
        self._stop.set()
        if self._sweeper is not None:
            self._sweeper.join(timeout=2)
