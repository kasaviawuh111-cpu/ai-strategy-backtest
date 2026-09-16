"""Bounded acceptance for the explicitly authorized preview release."""
import json
import time
import uuid
from pathlib import Path
import httpx

BASE = 'https://ashare-strategy-preview-305722-11-1330091763.sh.run.tcloudbase.com'
OUT = Path('outputs/public-release-20260916-guide')
REVISION = 'sha256:1c5ab52bcf144b70d53830a8b34428098fbf4bb93d50c1790ab4d658df1d8735'

def main():
    with httpx.Client(base_url=BASE, timeout=90, params={'release_check': 'guide-033'}, headers={
        'Origin': BASE, 'X-Release-Check': 'guide-033',
        'X-Preview-Client-ID': str(uuid.uuid4()),
    }) as client:
        def request(method, path, body=None):
            response = client.request(method, path, json=body, headers={
                'Prefer': 'respond-async', 'Idempotency-Key': str(uuid.uuid4())})
            deadline = time.monotonic() + 480
            location = response.headers.get('Location')
            while response.status_code == 202 and response.headers.get('X-Preview-Pending') == '1':
                location = response.headers.get('Location') or location
                if not location:
                    raise RuntimeError('Async response missing polling location')
                if time.monotonic() > deadline:
                    raise RuntimeError('Async result wait expired; do not resubmit')
                time.sleep(2)
                response = client.get(location)
            response.raise_for_status()
            return response.json()

        evidence = {}
        def save(key, value):
            evidence[key] = value
            (OUT / 'acceptance.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
            print(key, 'saved', flush=True)
            return value

        meta = save('meta', request('GET', '/api/v1/preview-meta'))
        assert REVISION in json.dumps(meta), 'Wrong deployed revision; refusing writes'
        save('ready', request('GET', '/api/v1/ready'))
        draft = save('draft', request('POST', '/api/v1/strategy-drafts', {
            'utterance': '平安银行，kdj超卖买入，然后10%止盈止损',
            'as_of_date': '2026-09-11',
        }))
        assert draft['status'] == 'ready', 'Draft did not reach ready'
        strategy = draft['strategy']
        assert strategy['instrument']['symbol'] == '000001.SZ'
        assert strategy['execution']['execution_resolution'] == '1m'
        body = {'strategy': strategy, 'config': {'runRobustness': False}}
        assert save('prepare', request('POST', '/api/v1/backtest-runs/prepare', body))['ready']
        run = save('submission', request('POST', '/api/v1/backtest-runs', body))
        path = '/api/v1/backtest-runs/' + run['id']
        deadline = time.monotonic() + 600
        while run['state'] not in ('succeeded', 'failed', 'cancelled'):
            if time.monotonic() > deadline:
                save('last_observation', run)
                raise RuntimeError('Run remains pending; inspect same run, do not resubmit')
            time.sleep(3)
            run = request('GET', path)
        save('terminal', run)
        assert run['state'] == 'succeeded', 'Backtest failed'
        for section in ('summary', 'series', 'trades'):
            value = save(section, request('GET', path + '/' + section))
            assert value, section + ' empty'
        assert evidence['summary']['runId'] == run['id']
        print('PASS', run['id'], flush=True)

if __name__ == '__main__':
    main()
