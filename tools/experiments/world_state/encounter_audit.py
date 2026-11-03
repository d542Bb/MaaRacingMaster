# -*- coding: utf-8 -*-
"""贴身事件审计（离线）：每次贴身事件打「极限/普通/撞」真值标签并归因。

标签组合（RULES §4.2.1）：极限超车必有横幅、普通超车无横幅、撞车零分。
- 横幅：trace raw_bonuses 计数（≥1 = 极限动作横幅在屏）
- 得分跳变：hud_rate 整秒增量超出里程底值 ≈ 速度 → +30/+150 通道
- 贴身：车轨迹 |x|<0.45 且 z<10 后消失，无 pass 事件 = 撞/侧刮嫌疑
归因：事件时段的 grid 伪车源（sel_dbg.opp.car 负 id）、sel_why、exec 横移。

用法：python tools/experiments/world_state/encounter_audit.py <session>
"""
import json, sys, math

def load(sess):
    p = (rf'C:/Users/yomen/AppData/Roaming/MaaRacingMaster/logs/{sess}'
         r'/trace/speedrush.jsonl')
    return [json.loads(l) for l in open(p, encoding='utf-8') if l.strip()]

def main(sess):
    rows = [r for r in load(sess) if 'fid' in r]
    # 得分跳变基线：hud_rate 是上一整秒总增量（含里程）。+30/+150 超出里程底值。
    rate = {r['fid']: r.get('hud_rate') for r in rows if r.get('hud_rate') is not None}
    banner = {r['fid']: (r.get('raw_bonuses') or 0) for r in rows}
    tracks, passf = {}, {}
    for r in rows:
        for e in (r.get('passes') or []):
            passf.setdefault(e['id'], []).append((r['fid'], e['d_min']))
        for t in (r.get('otab') or {}).get('t') or []:
            if t[0] == 'car':
                tracks.setdefault(t[1], []).append(
                    (r['fid'], t[2], t[4], r.get('sel_why'),
                     r.get('executed_lane')))
    segs = []
    for tid, tr in tracks.items():
        tr.sort(); cur = [tr[0]]
        for a, b in zip(tr, tr[1:]):
            if b[0] - a[0] > 30:
                segs.append((tid, cur)); cur = []
            cur.append(b)
        segs.append((tid, cur))
    print(f'{sess}: {len(rows)} 拍，{len(segs)} 段贴身轨迹')
    labels = {}
    for tid, seg in segs:
        if len(seg) < 4:
            continue
        near = [t for t in seg if t[1] is not None and abs(t[1]) < 0.45]
        zs = [t[2] for t in seg if t[2] is not None]
        if not near:
            continue
        f0, lf = near[0][0], seg[-1][0]
        zmin = min(zs) if zs else None
        pas = passf.get(tid)
        # 事件窗：首贴身前 1s 到消失后 2.5s
        w = [r for r in rows if f0 - 20 <= r['fid'] <= lf + 50]
        has_banner = any((banner.get(r['fid']) or 0) > 0 for r in w)
        # 得分通道：窗口内 hud_rate 峰值
        rates = [rate.get(r['fid']) for r in w if r.get('hud_rate') is not None]
        peak = max(rates) if rates else None
        if pas:
            dm = pas[0][1]
            label = ('极限超车' if 0.50 <= dm <= 0.77 else
                     '普通超车(宽)' if dm > 0.77 else '侧刮通过(过近)')
        elif has_banner:
            label = '横幅在屏(动作触发)'
        elif peak is not None and peak > 45:
            label = '得分通道+30(普通超车?)'
        elif zmin is not None and zmin < 10:
            label = '撞车嫌疑(无横幅无得分)'
        else:
            label = '其他'
        labels[tid] = label
        # 归因：事件窗内 grid 伪车源 / sel_why 分布 / exec 横移量
        g = sum(1 for r in w for c in ((r.get('sel_dbg') or {}).get('opp')
                or {}).get('car') or [] if c and c[0] is not None and c[0] < -3000)
        why = {}
        ex = [t[4] for t in seg if len(t) > 4 and t[4] is not None]
        exd = round(max(ex) - min(ex), 2) if ex else None
        for r in w:
            k = r.get('sel_why') or 'None'
            why[k] = why.get(k, 0) + 1
        top = sorted(why.items(), key=lambda x: -x[1])[:3]
        print(f'car{tid}: {label}  zmin={zmin and round(zmin,2)} d_min='
              f'{pas and round(pas[0][1],2)} grid源={g}次 exec横移={exd} '
              f'sel_why={top}')
    import collections
    print('\n标签分布:', dict(collections.Counter(labels.values())))

if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else '20261103_143052')
