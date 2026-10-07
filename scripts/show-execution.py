#!/usr/bin/env python3
"""Prints node outputs of an n8n execution, read straight from the n8n SQLite DB.

Usage: scripts/show-execution.py [execution_id|last] [node name ...]
Without node names, lists nodes with run counts and the execution error (if any).
"""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile

DOCKER = shutil.which('docker') or '/Applications/Docker.app/Contents/Resources/bin/docker'


def unflatten(arr):
    """Decodes the 'flatted' format n8n uses for execution data."""
    memo = {}

    def revive(i):
        if i in memo:
            return memo[i]
        v = arr[i]
        if isinstance(v, list):
            out = []
            memo[i] = out
            out.extend(revive(int(x)) if isinstance(x, str) else x for x in v)
            return out
        if isinstance(v, dict):
            out = {}
            memo[i] = out
            for k, x in v.items():
                out[k] = revive(int(x)) if isinstance(x, str) else x
            return out
        memo[i] = v
        return v

    return revive(0)


def main():
    exec_id = sys.argv[1] if len(sys.argv) > 1 else 'last'
    nodes = sys.argv[2:]
    tmp = tempfile.mkdtemp()
    for f in ('database.sqlite', 'database.sqlite-wal', 'database.sqlite-shm'):
        subprocess.run([DOCKER, 'cp', f'pavanatto-n8n:/home/node/.n8n/{f}', tmp], capture_output=True)
    db = sqlite3.connect(os.path.join(tmp, 'database.sqlite'))
    if exec_id == 'last':
        exec_id = db.execute('select max(id) from execution_entity').fetchone()[0]
    row = db.execute(
        'select e.status, e.startedAt, e.stoppedAt, d.data from execution_entity e '
        'join execution_data d on d.executionId = e.id where e.id = ?', (exec_id,)).fetchone()
    shutil.rmtree(tmp)
    if not row:
        sys.exit(f'execution {exec_id} not found')
    status, started, stopped, data = row
    rd = unflatten(json.loads(data))['resultData']
    run_data = rd.get('runData', {})

    if not nodes:
        print(f'execution {exec_id}: {status} ({started} -> {stopped})')
        if rd.get('error'):
            print('error:', rd['error'].get('message'))
        for name, runs in run_data.items():
            items = (runs[-1].get('data') or {}).get('main') or [[]]
            print(f'  {name}: runs={len(runs)} items={sum(len(o or []) for o in items)}')
        return

    out = {}
    for name in nodes:
        runs = run_data.get(name) or [{}]
        main = (runs[-1].get('data') or {}).get('main') or [[]]
        out[name] = [it['json'] for o in main for it in (o or [])]
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
