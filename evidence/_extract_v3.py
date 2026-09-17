import json, datetime, re, sys

def ms2iso(ms):
    return datetime.datetime.fromtimestamp(ms/1000, datetime.timezone.utc).strftime('%H:%M:%S')

def load(path):
    with open(path, encoding='utf-8') as f:
        data = json.load(f)
    return data if isinstance(data, list) else (data.get('events') or [])

def dump_events(events, out_pat, lo, hi):
    msgs = [e for e in events if e.get('type') == 'm.room.message']
    win = [e for e in msgs if lo <= e['origin_server_ts'] <= hi]
    with open(out_pat, 'w', encoding='utf-8') as f:
        for e in win:
            body = (e.get('content') or {}).get('body', '')
            body1 = ' '.join(body.split())
            f.write(f"{ms2iso(e['origin_server_ts'])} | {e['event_id']} | {e['sender']} | {body1[:400]}\n")
    return len(win)

P2 = 'FINALS-ELEM-PR2-V3-TRACED'
P3 = 'FINALS-ELEM-PR3-V3-TRACED'

def ts(h, m, s):
    d = datetime.datetime(2026, 9, 17, h, m, s, tzinfo=datetime.timezone.utc)
    return int(d.timestamp()*1000)

# PR2 window: kickoff 03:02:46Z -> final 03:15:18Z (+ slack)
team2 = load(f'{P2}/team-room-messages.json')
dm2 = load(f'{P2}/leader-dm-messages.json')
n1 = dump_events(team2, '_p2_team.txt', ts(3,0,0), ts(3,20,0))
n2 = dump_events(dm2, '_p2_dm.txt', ts(3,0,0), ts(3,20,0))
print(f'PR2 team msgs in window: {n1}, dm msgs in window: {n2}')

# PR3 window: kickoff 03:19:03Z -> final 03:21:14Z (+ slack)
team3 = load(f'{P3}/team-room-messages-pr3v3-window.json')
dm3 = load(f'{P3}/leader-dm-messages-pr3v3-window.json')
n3 = dump_events(team3, '_p3_team.txt', ts(3,15,0), ts(3,25,0))
n4 = dump_events(dm3, '_p3_dm.txt', ts(3,15,0), ts(3,25,0))
print(f'PR3 team msgs in window: {n3}, dm msgs in window: {n4}')
