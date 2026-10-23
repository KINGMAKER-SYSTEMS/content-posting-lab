#!/bin/bash
# Prepared only. Do not start until the photo integration and release gates pass.
set -eu
exec python3 - <<'PY'
import datetime as dt
import json
import pathlib
import re
import subprocess
import time
import urllib.request
from zoneinfo import ZoneInfo

SEATS = pathlib.Path('/Users/ecfromthedc/dev/seats')
LOG = pathlib.Path('/Users/ecfromthedc/dev/control-plane/logs/lab-photo-merge-1024-1140.out')
HOLDS = pathlib.Path('/Users/ecfromthedc/dev/control-plane/logs/coord1022-holds.txt')
REPO = 'KINGMAKER-SYSTEMS/content-posting-lab'
SERVICE = 'a6dcd562-df4c-4ae1-a4e6-414f0d61b74c'
ENVIRONMENT = '99ff0f13-254f-4bf2-960a-134f4f2e0e0f'
TARGET = dt.datetime(2026, 10, 24, 11, 40, 30, tzinfo=ZoneInfo('America/New_York')).timestamp()

def log(message):
    with LOG.open('a') as f:
        f.write(dt.datetime.now(dt.timezone.utc).isoformat() + ' ' + message + '\n')

def abort(message):
    log('ABORT ' + message)
    raise SystemExit(3)

def gh(*args):
    return subprocess.check_output(['gh', *args], text=True, timeout=90).strip()

def check_gap():
    if not TARGET <= time.time() <= TARGET + 600:
        abort('outside the gap or more than 10 minutes late')

def check_holds(pr):
    # Missing/unreadable coordination state refuses release.
    lines = HOLDS.read_text().splitlines()
    for line in lines:
        fields = line.split()
        if len(fields) >= 2 and fields[0] == 'HOLD' and fields[1] in {str(pr), 'photo', 'lab-photo', 'lab'}:
            abort('held: ' + line)

def deployments():
    config = json.loads((pathlib.Path.home() / '.railway/config.json').read_text())
    user = config['user']
    token = user.get('accessToken') or user.get('token')
    if not token:
        raise ValueError('Railway credential unavailable')
    query = 'query($s:String!,$e:String!){deployments(first:100,input:{serviceId:$s,environmentId:$e}){edges{node{id status createdAt meta}}}}'
    request = urllib.request.Request('https://backboard.railway.com/graphql/v2',
        data=json.dumps({'query': query, 'variables': {'s': SERVICE, 'e': ENVIRONMENT}}).encode(),
        headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json', 'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(request, timeout=30) as response:
        result = json.load(response)
    if result.get('errors'):
        raise ValueError('Railway query failed')
    return [edge['node'] for edge in result['data']['deployments']['edges']]

# The current task is blocked. No ready receipt is supplied by this script.
# A future exact-head gate must explicitly attest all four checks after integration.
try:
    pr = int((SEATS / 'lab-photo-pr.txt').read_text().strip())
    expected = (SEATS / 'lab-photo-expected-head.txt').read_text().strip()
    if not re.fullmatch('[0-9a-f]{40}', expected):
        abort('invalid pinned head')
    gate = json.loads((SEATS / 'lab-photo-ready.json').read_text())
    required = ('composed_review', 'base_head_suite', 'real_media_storage_readback', 'renderer_admission_receipts')
    if gate.get('head') != expected or gate.get('status') != 'READY' or any(gate.get(k) is not True for k in required):
        abort('photo integration gates not passed for pinned head')
    while time.time() < TARGET:
        time.sleep(min(30, TARGET - time.time()))
    check_gap()
    check_holds(pr)
    view = json.loads(gh('pr', 'view', str(pr), '-R', REPO, '--json', 'headRefOid,mergeable,state,baseRefName'))
    if view['headRefOid'] != expected or view['mergeable'] != 'MERGEABLE' or view['state'] != 'OPEN' or view['baseRefName'] != 'main':
        abort('PR head, mergeability, state or base changed')
    before = {d['id'] for d in deployments()}
    check_gap()
    check_holds(pr)
    gh('pr', 'ready', str(pr), '-R', REPO)
    check_gap()
    check_holds(pr)
    gh('pr', 'merge', str(pr), '-R', REPO, '--squash', '--match-head-commit', expected)
except SystemExit:
    raise
except Exception as error:
    # Never print credential-bearing exception/response bodies.
    abort('pre-merge failure: ' + type(error).__name__)

# Watch only. Never trigger a deployment or accept an old deployment.
end = time.monotonic() + 1200
try:
    merged = json.loads(gh('pr', 'view', str(pr), '-R', REPO, '--json', 'state,mergeCommit'))
    sha = (merged.get('mergeCommit') or {}).get('oid', '')
    if merged.get('state') != 'MERGED' or not re.fullmatch('[0-9a-f]{40}', sha):
        raise ValueError('merge commit unavailable')
    log('LAB MERGED ' + sha)
    while time.monotonic() < end:
        try:
            for deployment in deployments():
                meta = deployment.get('meta') or {}
                if isinstance(meta, str):
                    meta = json.loads(meta)
                created = dt.datetime.fromisoformat(deployment['createdAt'].replace('Z', '+00:00')).timestamp()
                if deployment['id'] not in before and created >= TARGET and meta.get('commitHash') == sha and deployment['status'] == 'SUCCESS' and time.monotonic() <= end:
                    log('LAB OK ' + deployment['id'] + ' ' + sha)
                    raise SystemExit(0)
        except (ValueError, KeyError, TypeError, OSError):
            pass
        time.sleep(min(20, max(0, end - time.monotonic())))
except SystemExit:
    raise
except Exception:
    pass
log('LAB NOT BUILT')
raise SystemExit(4)
PY
