"""Real candidate search and follow-up checks; no backtests submitted."""
import json
import time
import uuid
import sys
from pathlib import Path
import httpx

OUT = Path('outputs/public-release-20260916-guide')
BASE = 'https://ashare-strategy-preview-305722-11-1330091763.sh.run.tcloudbase.com'

def main():
    prior = json.loads((OUT / 'acceptance.json').read_text())['draft']
    with httpx.Client(base_url=BASE, params={'release_check': 'guide-033'},
                      timeout=90, headers={'Origin': BASE}) as client:
        def post(body, parent=None):
            headers = {'Prefer': 'respond-async', 'Idempotency-Key': str(uuid.uuid4())}
            if parent:
                headers['X-Conversation-Parent-Draft-ID'] = parent
            r = client.post('/api/v1/strategy-drafts', json=body, headers=headers)
            location = r.headers.get('Location')
            deadline = time.monotonic() + 480
            while r.status_code == 202 and r.headers.get('X-Preview-Pending') == '1':
                assert location and time.monotonic() < deadline, 'Polling cannot continue; do not resubmit'
                time.sleep(2)
                r = client.get(location)
                location = r.headers.get('Location') or location
            r.raise_for_status()
            return r.json()
        if '--search-only' not in sys.argv:
            follow = post({'utterance': '止盈改成12%，其他保持不变',
                       'as_of_date': '2026-09-11', 'edit_current_strategy': True}, prior['draft_id'])
            (OUT / 'follow-up.json').write_text(json.dumps(follow, ensure_ascii=False, indent=2))
            assert follow['status'] == 'ready', follow.get('status')
            assert follow['strategy']['entry'] == prior['strategy']['entry']
            protection = follow['strategy']['exit']['children'][0]
            assert float(protection['take_profit_pct']) == 12 and float(protection['stop_loss_pct']) == 10
            print('follow-up PASS', flush=True)
        search = post({'utterance': '最近固态电池产业有什么新进展？我觉得这个方向可能有机会，帮我先核实消息。',
                       'as_of_date': '2026-09-11'})
        (OUT / 'viewpoint-search.json').write_text(json.dumps(search, ensure_ascii=False, indent=2))
        print('search saved', search.get('status'), flush=True)

if __name__ == '__main__':
    main()
