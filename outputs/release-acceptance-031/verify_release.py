"""Bounded public acceptance against the exact reviewed package; no secret output."""
import hashlib
import json
import sys
import time
from pathlib import Path
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).parent
BASE = 'https://ashare-strategy-preview-305722-11-1330091763.sh.run.tcloudbase.com'
BUNDLE = Path(sys.argv[1])
manifest = json.loads((BUNDLE / 'source-manifest.json').read_text())
revision = manifest['codeRevision']


def save(name, value):
    (OUT / (name + '.json')).write_text(json.dumps(value, ensure_ascii=False, indent=2))


with httpx.Client(base_url=BASE, timeout=60, headers={
    'Origin': BASE, 'X-Preview-Client-ID': str(uuid4()),
}) as client:
    def get(path):
        r = client.get(path)
        r.raise_for_status()
        return r.json()

    def post(path, body):
        start = time.monotonic()
        r = client.post(path, json=body, headers={
            'Prefer': 'respond-async', 'Idempotency-Key': str(uuid4()),
        })
        initial_http, initial_seconds = r.status_code, round(time.monotonic() - start, 2)
        while r.headers.get('X-Preview-Pending') == '1':
            assert time.monotonic() - start < 360, 'async request exceeded acceptance deadline'
            location = r.headers['Location']
            assert location.startswith('/api/v1/preview-requests/')
            time.sleep(1)
            r = client.get(location)
        r.raise_for_status()
        return {'initial_http': initial_http, 'initial_seconds': initial_seconds,
                'http': r.status_code, 'body': r.json()}

    meta = get('/api/v1/preview-meta')
    assert meta['revision'] == revision, meta
    ready = get('/api/v1/ready')
    assert ready['status'] == 'ready'
    asset_checks = []
    for row in manifest['files']:
        if not (row['path'].startswith('web/dist/assets/') or row['path'] == 'web/dist/index.html'):
            continue
        route = '/' if row['path'].endswith('/index.html') else '/' + row['path'].removeprefix('web/dist/')
        r = client.get(route)
        r.raise_for_status()
        assert hashlib.sha256(r.content).hexdigest() == row['sha256'], route
        asset_checks.append(route)
    save('identity', {'meta': meta, 'ready': ready, 'matchingAssets': asset_checks})
    print('IDENTITY_AND_ASSETS_PASS', revision, flush=True)

    draft = post('/api/v1/strategy-drafts', {
        'utterance': '东财金叉买，死叉卖', 'as_of_date': '2026-09-15',
    })
    save('alias-draft', draft)
    body = draft['body']
    assert draft['http'] == 201 and body['status'] == 'ready', body.get('diagnostic_code')
    assert body['strategy']['instrument']['symbol'] == '300059.SZ'
    assert body['strategy']['entry']['indicator_id'] == 'technical.macd'
    assert 'death_cross' in json.dumps(body['strategy']['exit'])
    print('ALIAS_DRAFT_PASS', flush=True)

    # Execute the same real-model plan verified locally. Preserve all dates/rules.
    source = ROOT / 'outputs/single-strategy-order-regression-20260915/alias-whitespace-breakout-fixed/spaced_name_breakout.json'
    strategy = json.loads(source.read_text())['response']['strategy']
    queued = post('/api/v1/backtest-runs', {'strategy': strategy})
    save('backtest-submission', queued)
    assert queued['http'] == 202
    run = queued['body']
    deadline = time.monotonic() + 300
    while run['state'] not in ('succeeded', 'failed', 'cancelled') and time.monotonic() < deadline:
        time.sleep(2)
        run = get('/api/v1/backtest-runs/' + run['id'])
    save('backtest-terminal', run)
    assert run['state'] == 'succeeded', run
    reports = {}
    for name in ('summary', 'series', 'trades'):
        reports[name] = get(f"/api/v1/backtest-runs/{run['id']}/{name}")
        save(name, reports[name])
    evidence = reports['summary']['runEvidence']
    assert evidence['codeRevision'] == revision
    assert evidence['strategyHash'] == json.loads(source.read_text())['response']['strategy_hash']
    fills = [r for r in reports['trades'] if r['kind'] in ('fill', 'partial_fill')]
    assert reports['series'] and fills and fills[0]['side'] == 'buy'
    assert not any(r['side'] == 'sell' and r['occurredAt'] < fills[0]['occurredAt'] for r in fills)
    result = {'revision': revision, 'runId': run['id'], 'state': run['state'],
              'points': len(reports['series']), 'fills': len(fills),
              'snapshot': evidence['dataSnapshotId'], 'strategyHash': evidence['strategyHash'],
              'asyncAccepted': draft['initial_http'] == 202 and queued['initial_http'] == 202}
    save('acceptance', result)
    print(json.dumps(result, ensure_ascii=False), flush=True)
