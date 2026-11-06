# -*- coding: utf-8 -*-
"""贴身事件审计（离线）：撞车/贴身按速度塌陷打标签并归因。

判据（RULES §4.2.1/§4.3/§4.9）：
- 撞车不加分不扣分，唯一代价是浪费时间 → 里程得分增速（hud_rate）塌陷是撞/压车的
  直接特征；血量由双方积分差决定（积分是因血量是果），不可用作撞车判据。
- 起步 / 阶段切换后重新起步同样塌陷：增速从谷底单调爬升、无 V 形 → 排除（帧证
  2026-11-03：倒计时/「阶段切换」横幅两例均为假阳）。
- 贴身轨迹（otab car）仍按横幅×得分跳变×pass 分带打「极限/普通/侧刮」标签；
  被静默跳过的段计数列出，不再无声丢弃。
  通过宽度（20261104_085659 帧证改口径）：pass 事件的寿命 d_min 是整条轨迹
  最小 |x|，被「远场同道/路中接近段」污染——15 起标「侧刮」里 13 起的极小
  d_min 取在 z=20-85m（车还在几十米外），真贴近时 0.27-1.97 道是正常通过；
  另有 stale 轨迹冻结读数冒充贴身。标签改用**近距窗（z<15m）最小 |x|**
  （像素尺优先米制兜底），从未进入近距的段不按 d_min 定罪。
归因：事件窗内近距车（同道 |x|<0.5 / 邻道）、grid 伪车源次数、sel_why 分布。

用法：python tools/experiments/world_state/encounter_audit.py <session>
"""
import json, sys, math, os

RATE_BASE = 41.0   # 里程得分增速底值（≈速度）
RATE_LOW = 38.0    # 低于此为塌陷
GAP_FID = 15       # 塌陷段切分间隔（帧）
SKIP_FID = 30      # 轨迹段切分间隔（帧）

# 车道宽（米）从真源 gate0.json 读，不写字面量（数值禁令）
_GATE0 = os.path.join(os.path.dirname(__file__), '..', '..', '..',
                      'maaracing_master', 'plugins', 'speedrush',
                      'resources', 'calibration', 'gate0.json')
LANE_W_M = json.load(open(_GATE0, encoding='utf-8'))['lane_w_m']

def lane_x(x_lane, x_m):
    """横向（车道单位）：像素尺子优先，缺时用米制横向换算——远车尺子被
    域闸挡掉恒 None（20261103_153620 局：9 起盲区嫌疑大半是这个假象）。"""
    if x_lane is not None:
        return x_lane
    return None if x_m is None else x_m / LANE_W_M

def split_races(rows):
    """fid 重置点切局（同一 trace 多局 fid 重叠，按 tid 聚合会把不同局的
    同名车混进一条轨迹——20261104_085659 实证）。"""
    races, cur = [], [rows[0]]
    for a, b in zip(rows, rows[1:]):
        if b['fid'] < a['fid']:
            races.append(cur)
            cur = []
        cur.append(b)
    races.append(cur)
    return races

def load(sess):
    p = os.path.expandvars(rf'%APPDATA%/MaaRacingMaster/logs/{sess}'
                           r'/trace/speedrush.jsonl')
    return [json.loads(l) for l in open(p, encoding='utf-8') if l.strip()]

def dips(rows):
    """速度塌陷段：[(fid0, fid1, rate_min, rate序列)]"""
    pts = [(r['fid'], r['hud_rate']) for r in rows
           if r.get('hud_rate') is not None and r['hud_rate'] < RATE_LOW]
    segs, cur = [], []
    for a in pts:
        if cur and a[0] - cur[-1][0] > GAP_FID:
            segs.append(cur); cur = []
        cur.append(a)
    if cur:
        segs.append(cur)
    return [(s[0][0], s[-1][0], min(v for _, v in s), [v for _, v in s])
            for s in segs if (s[-1][0] - s[0][0]) >= 10]

def race_body(ri, race, counter):
    """单局审计：速度塌陷事件 + 贴身轨迹标签（mutates counter 汇总）。"""
    rows = race
    rate = {r['fid']: r.get('hud_rate') for r in rows if r.get('hud_rate') is not None}
    banner = {r['fid']: (r.get('raw_bonuses') or 0) for r in rows}
    stage = {r['fid']: r.get('stage_id') for r in rows}
    tracks, passf = {}, {}
    for r in rows:
        for e in (r.get('passes') or []):
            passf.setdefault(e['id'], []).append((r['fid'], e['d_min']))
        for t in (r.get('otab') or {}).get('t') or []:
            if t[0] == 'car':
                tracks.setdefault(t[1], []).append(
                    (r['fid'], lane_x(t[2], t[3] if len(t) > 3 else None),
                     t[4], r.get('sel_why'), r.get('executed_lane')))
    segs = []
    for tid, tr in tracks.items():
        tr.sort(key=lambda x: x[0]); cur = [tr[0]]
        for a, b in zip(tr, tr[1:]):
            if b[0] - a[0] > SKIP_FID:
                segs.append((tid, cur)); cur = []
            cur.append(b)
        segs.append((tid, cur))
    print(f'\n--- 局{ri+1}: {len(rows)} 拍，{len(segs)} 段贴身轨迹，'
          f'{len(dips(rows))} 段速度塌陷 ---')

    # ── 速度塌陷事件 ──────────────────────────────────────────
    car_by_fid = {r['fid']: [(t[1], lane_x(t[2], t[3] if len(t) > 3 else None), t[4])
                             for t in (r.get('otab') or {}).get('t') or []
                             if t[0] == 'car'] for r in rows}
    print('\n== 速度塌陷事件（撞/压车主判据）==')
    launch_n = same_n = blind_n = graze_n = 0
    for f0, f1, rmin, series in dips(rows):
        # 起步/切换：多级渐进爬升（≥3 个递增值，帧证 1100 起步 [0,6,20,27,31]、
        # 2780 商店切换后 [19,24,29] 两例）；撞/压车是 ≤2 个低位值直接回底值
        rl = [series[0]]
        for v in series:
            if v != rl[-1]:
                rl.append(v)
        launch = (len(rl) >= 3 and
                  all(b >= a for a, b in zip(rl, rl[1:])))
        w = [f for f in range(f0 - 40, f1 + 1) if f in car_by_fid]
        near = [(cid, xx, zz) for f in w for cid, xx, zz in car_by_fid[f]
                if zz is not None and zz < 15]
        same = [n for n in near if n[1] is not None and abs(n[1]) < 0.5]
        # 浅塌分界（20261103_213728 帧证校准）：rate_min 34-37 且无同道
        # = 贴身通过的正常速度代价（3150 帧蓝车擦身），不算事故；
        # 真撞/压 = 深塌（≤32）或同道在场
        graze_shallow = (rmin > 32 and not same)
        tag = '起步/切换加速' if launch else (
            '贴身通过浅塌' if graze_shallow else (
                '撞/压同道车' if same else '撞车嫌疑（近距无同道车→盲区）'))
        if launch:
            launch_n += 1
        elif graze_shallow:
            graze_n += 1
        elif same:
            same_n += 1
        else:
            blind_n += 1
        ids = sorted(set(n[0] for n in near))[:6]
        print(f'fid {f0}-{f1} ({(f1-f0)/20:.1f}s) rate_min={rmin:.0f} {tag}'
              f' 近距车={ids} 同道={len(same)}')
    counter['launch'] += launch_n; counter['same'] += same_n
    counter['blind'] += blind_n; counter['graze'] += graze_n

    # ── 贴身轨迹标签 ──────────────────────────────────────────
    print('\n== 贴身轨迹标签（横幅×得分跳变×近距通过窗）==')
    labels, skipped = {}, 0
    for tid, seg in segs:
        if len(seg) < 4:
            skipped += 1
            continue
        near = [t for t in seg if t[1] is not None and abs(t[1]) < 0.45]
        zs = [t[2] for t in seg if t[2] is not None]
        if not near:
            skipped += 1
            continue
        f0, lf = near[0][0], seg[-1][0]
        zmin = min(zs) if zs else None
        # 通过宽度：近距窗（3≤z<15m）最小 |x|——寿命 d_min 被远场同道接近段
        # 污染（车还在几十米外就读到 ≈0），z<3m 又落贴脸平面崩坏带（car66
        # 帧证：z<3 读 0.004 道，z 3-15 读 0.18 道，目测间隙 0.3-0.5m）。
        close = [abs(t[1]) for t in seg
                 if t[1] is not None and t[2] is not None and 3 <= t[2] < 15]
        dm_close = min(close) if close else None
        pas = passf.get(tid)
        dm_life = min(d for _, d in pas) if pas else None
        w = [r for r in rows if f0 - 20 <= r['fid'] <= lf + 50]
        has_banner = any((banner.get(r['fid']) or 0) > 0 for r in w)
        rates = [rate.get(r['fid']) for r in w if rate.get(r['fid']) is not None]
        peak = max(rates) if rates else None
        if dm_close is not None:
            label = ('极限超车' if 0.50 <= dm_close <= 0.77 else
                     '普通超车(宽)' if dm_close > 0.77 else '侧刮通过(过近)')
        elif has_banner:
            label = '横幅在屏(动作触发)'
        elif peak is not None and peak > RATE_BASE + 4:
            label = '得分通道+30(普通超车?)'
        else:
            label = '贴身无得分（撞/压嫌疑）'
        labels[f'局{ri+1}car{tid}'] = label
        g = sum(1 for r in w for c in ((r.get('sel_dbg') or {}).get('opp')
                or {}).get('car') or [] if c and c[0] is not None and c[0] < -3000)
        why = {}
        for r in w:
            k = r.get('sel_why') or 'None'
            why[k] = why.get(k, 0) + 1
        top = sorted(why.items(), key=lambda x: -x[1])[:3]
        ex = [t[4] for t in seg if len(t) > 4 and t[4] is not None]
        exd = round(max(ex) - min(ex), 2) if ex else None
        print(f'局{ri+1}car{tid}: {label}  zmin={zmin and round(zmin,2)} '
              f'd_min(近距)={"None" if dm_close is None else round(dm_close,3)} '
              f'd_min(寿命)={"None" if dm_life is None else round(dm_life,3)} '
              f'grid源={g}次 exec横移={exd} '
              f'sel_why={top}')
    print(f'\n局{ri+1} 跳过段：{skipped}（短于 4 拍或无近距横距，不再无声丢弃）')
    for k, v in labels.items():
        counter[v] = counter.get(v, 0) + 1

def main(sess):
    rows = [r for r in load(sess) if 'fid' in r]
    races = split_races(rows)
    print(f'{sess}: {len(races)} 局（fid 重置分段），拍数 {[len(x) for x in races]}')
    counter = {'launch': 0, 'same': 0, 'blind': 0, 'graze': 0}
    for ri, race in enumerate(races):
        race_body(ri, race, counter)
    print(f'\n塌陷分类（两局合计）：起步/切换 {counter["launch"]}，'
          f'撞/压同道车 {counter["same"]}，盲区嫌疑 {counter["blind"]}，'
          f'贴身浅塌 {counter["graze"]} → 撞/压事件合计 '
          f'{counter["same"] + counter["blind"]}')
    import collections
    dist = {k: v for k, v in counter.items()
            if k not in ('launch', 'same', 'blind', 'graze')}
    print('贴身标签分布:', dict(collections.Counter(dist)))

if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else '20261103_143052')
