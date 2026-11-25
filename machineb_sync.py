"""Allowlist-only differential file synchronization; never mirror or delete.

Callers supply explicit destinations. All local files are read before any target
operation. A successful result includes hash verification for skipped files too.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path, PureWindowsPath
from typing import Any, Mapping, Sequence


class SyncError(RuntimeError):
    def __init__(self, phase: str, message: str, artifact: Mapping[str, Any]):
        self.phase = phase
        self.remote_path = str(artifact.get('remote_path', ''))
        self.artifact_kind = str(artifact.get('kind', 'file'))
        super().__init__(f'{phase}: {self.remote_path}: {message}')


def preflight_files(artifacts: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    prepared = []
    seen = set()
    for source in artifacts:
        item = dict(source)
        remote = PureWindowsPath(str(item['remote_path']))
        key = str(remote).casefold()
        if key in seen:
            raise SyncError('plan_conflict', 'duplicate Windows destination', item)
        seen.add(key)
        if not remote.is_absolute() or '..' in remote.parts:
            raise SyncError('plan_conflict', 'destination must be an absolute Windows file path without traversal', item)
        item['remote_path'] = str(remote)
        try:
            item['sha256'] = hashlib.sha256(Path(item['local_path']).read_bytes()).hexdigest()
        except (OSError, ValueError) as exc:
            raise SyncError('local_preflight', str(exc), item) from exc
        prepared.append(item)
    return prepared


def _hash(transport: Any, item: Mapping[str, Any]) -> str:
    try:
        digest = str(transport.remote_sha256(item['remote_path'])).strip().lower()
        if not re.fullmatch(r'[0-9a-f]{64}', digest):
            raise ValueError('invalid SHA256 response')
        return digest
    except Exception as exc:
        raise SyncError('hash_read', str(exc), item) from exc


def sync_files(artifacts: Sequence[Mapping[str, Any]], transport: Any) -> dict[str, Any]:
    items = preflight_files(artifacts)
    results = []
    for item in items:
        try:
            exists = transport.remote_file_exists(item['remote_path'])
        except Exception as exc:
            raise SyncError('hash_read', f'cannot inspect destination: {exc}', item) from exc
        identical = exists and _hash(transport, item) == item['sha256']
        if not identical:
            try:
                transport.ensure_directories([str(PureWindowsPath(item['remote_path']).parent)])
            except Exception as exc:
                raise SyncError('directory_setup', str(exc), item) from exc
            try:
                transport.upload(Path(item['local_path']), item['remote_path'])
            except Exception as exc:
                raise SyncError('upload', str(exc), item) from exc
            if _hash(transport, item) != item['sha256']:
                raise SyncError('hash_mismatch', 'uploaded SHA256 differs from local source', item)
        results.append(dict(item, action='skipped' if identical else 'uploaded', hash_verified=True))
    return {
        'uploaded_count': sum(item['action'] == 'uploaded' for item in results),
        'skipped_count': sum(item['action'] == 'skipped' for item in results),
        'verified_count': len(results),
        'artifacts': results,
    }
