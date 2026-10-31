# -*- coding: utf-8 -*-
"""选卡圆角几何的验收测量：把「CSS 画出来的形状」与「我们以为的形状」对上账。

为什么需要它
------------
选卡卡片用 ``border-radius`` + ``corner-shape: squircle`` 画轮廓，描边则由
``js/gallery-shape.js`` 按同一条数学定义（指数 4 的超椭圆）单独生成路径。这是
**一个方程、两个渲染器**：CSS 那个在浏览器里，JS 那个在我们手里。只要浏览器改了
超椭圆的实现，描边就会从边缘漂开——这种漂移是渐进的，肉眼要等它到 1px 以上才看得出，
所以必须能定量复测，而不是等用户说「怎么歪了」。

测什么
------
1. **形状律**（大半径 R160，高信噪比）：轮廓是否仍是 ``|u|⁴+|v|⁴=1``。
   半径取大是为了让抗锯齿的相对误差变小——同样的 0.3px 偏差，在 R=160 上是 0.2%，
   在 R=20 上是 1.5%，前者才测得准。
2. **面积比**（只在 R160 上判定）：角部切掉的面积 ÷ 理论值。面积是「圆润度」的积分，
   比单点偏差更容易发现「整体变软/变方」。
3. **实际半径下的轮廓**（R20，即选卡卡片真实用的值）：只判轮廓偏差、不判面积——
   R20 时抗锯齿本身就有 ~0.2px 的偏置，面积比会被它带偏 7%，拿它当验收线只会误报。

测量坐标系与 DPR（**别把设备像素当 CSS 像素**）
----------------------------------------------
探针页用 CDP 的 ``Emulation.setDeviceMetricsOverride(deviceScaleFactor=SCALE)`` 固定采样率，
**不用 CSS zoom**——zoom 会改布局尺寸，和 captureBeyondViewport 叠加后实际倍率不可控
（踩过：写 zoom=3 实际拿到 2.8×，所有长度整体偏 7%）。
换算只发生在 ``measure_box()`` 一处：设备像素 ÷ SCALE = CSS px。别在别处再除一次。

不做什么
--------
- 不跑 CI：需要桌面会话（起一个可见的 Chromium 窗口 + CDP），无头模式在部分机器上不可用。
- 不测动效、不测布局、不做视觉回归——只看拐角这一条曲线。
- 不改任何生产文件，探针页与截图都落在临时目录。

用法
----
    python tools/measure_corner_shape.py            # 跑验收，打印表格
    python tools/measure_corner_shape.py --keep     # 保留探针页与截图（排查用）
"""
from __future__ import annotations

import argparse
import base64
import json
import math
import os
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

# ---- 验收阈值（改这里就是改验收标准，改完请同步 README 与测试） ----
MAX_DEV_PX = 0.5      # 轮廓最大逐行偏差，CSS px
AREA_RATIO = 0.05     # 角部面积比允许偏离 1 的幅度（±5%），只在大半径样本上判
SCALE = 2             # 截图采样率：1 CSS px = SCALE 设备像素
BOX_W, BOX_H = 360, 340   # 高度必须 ≥ 2R：半径重叠时浏览器会按比例缩小全部圆角
PAD = 10

# 名字 / CSS 形状 / 半径 / 理论指数 / 是否判面积
CASES = [
    ("round R160（对照）", "round", 160.0, 2, True),
    ("squircle R160（形状律）", "squircle", 160.0, 4, True),
    ("squircle R20（选卡实际值）", "squircle", 20.0, 4, False),
]

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
]


# ---------------------------------------------------------------- 理论曲线
def se_x(R: float, n: float, y: float) -> float:
    """指数 n 的超椭圆（角框 R×R）在距顶边 y 处的横向内缩，单位 CSS px。

    |u|^n + |v|^n = 1，u = (R−x)/R，v = (R−y)/R；n=2 是圆弧，n=4 是 CSS 的 squircle。
    """
    if y >= R:
        return 0.0
    v = max(0.0, min(1.0, (R - y) / R))
    return R - R * (1.0 - v ** n) ** (1.0 / n)


def se_area(R: float, n: float) -> float:
    """角部切掉面积 ∫₀ᴿ x(y) dy，单位 CSS px²。"""
    steps = 2000
    h = R / steps
    return sum(se_x(R, n, (i + 0.5) * h) for i in range(steps)) * h


def se_dxdy(R: float, n: float, y: float) -> float:
    """dx/dy（解析式）。用来把「横向偏差」折算成「到曲线的垂直距离」。

    为什么必须折算：超椭圆在切点处切线是水平的，dx/dy → −∞，于是同一个 0.2px 的
    纵向对位误差会被放大成好几 px 的横向差。直接比横向值会把这个测量噪声当成
    「形状不符」——垂直距离才是「边缘偏离了多少」的真实答案。
    """
    if y >= R:
        return 0.0
    v = max(1e-9, min(1.0, (R - y) / R))
    if v >= 1.0:
        return -1e6
    return -((1.0 - v ** n) ** (1.0 / n - 1.0)) * (v ** (n - 1.0))


# ---------------------------------------------------------------- 探针页
def build_probe(path: Path) -> None:
    rows = []
    for i, (_, shape, R, _n, _a) in enumerate(CASES):
        extra = "border-radius:%gpx;" % R
        if shape == "squircle":
            extra += "corner-shape:squircle;"
        rows.append(f'<div class="box" id="b{i}" style="{extra}"></div>')
    html = (
        "<!doctype html><html><head><meta charset='utf-8'><style>\n"
        "  html,body{margin:0;background:#fff}\n"
        f"  body{{padding:{PAD}px;width:{BOX_W + 2 * PAD}px;box-sizing:border-box}}\n"
        f"  .box{{width:{BOX_W}px;height:{BOX_H}px;background:#000;margin:0 0 12px}}\n"
        "</style></head><body>\n" + "\n".join(rows) + "\n</body></html>\n"
    )
    path.write_text(html, encoding="utf-8")


# ---------------------------------------------------------------- 最小 CDP 客户端
def _ws_connect(host: str, port: int, path: str, timeout: float = 10.0) -> socket.socket:
    sock = socket.create_connection((host, port), timeout=timeout)
    key = base64.b64encode(os.urandom(16)).decode()
    sock.sendall((
        f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
        "Upgrade: websocket\r\nConnection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
    ).encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise RuntimeError("WebSocket 握手失败：连接被关闭")
        buf += chunk
    if b"101" not in buf.split(b"\r\n")[0]:
        raise RuntimeError("WebSocket 握手被拒：" + buf.split(b"\r\n")[0].decode("latin1"))
    return sock


def _ws_send(sock: socket.socket, msg: str) -> None:
    data = msg.encode()
    n = len(data)
    hdr = bytearray([0x81])
    if n < 126:
        hdr.append(0x80 | n)
    elif n < 65536:
        hdr.append(0x80 | 126)
        hdr += struct.pack(">H", n)
    else:
        hdr.append(0x80 | 127)
        hdr += struct.pack(">Q", n)
    mask = os.urandom(4)
    hdr += mask
    sock.sendall(bytes(hdr) + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(min(65536, n - len(buf)))
        if not chunk:
            raise EOFError("CDP 连接关闭")
        buf += chunk
    return buf


def _ws_recv(sock: socket.socket) -> str:
    payload = b""
    while True:
        h = _recv_exact(sock, 2)
        fin, op, ln = h[0] & 0x80, h[0] & 0x0F, h[1] & 0x7F
        if ln == 126:
            ln = struct.unpack(">H", _recv_exact(sock, 2))[0]
        elif ln == 127:
            ln = struct.unpack(">Q", _recv_exact(sock, 8))[0]
        payload += _recv_exact(sock, ln) if ln else b""
        if fin:
            if op == 0x8:
                raise EOFError("CDP 对端关闭")
            return payload.decode("utf-8", "replace")


def _call(sock: socket.socket, mid: int, method: str, params: dict | None = None) -> dict:
    _ws_send(sock, json.dumps({"id": mid, "method": method, "params": params or {}}))
    while True:
        msg = json.loads(_ws_recv(sock))
        if msg.get("id") == mid:
            if "error" in msg:
                raise RuntimeError(f"{method} 失败：{msg['error']}")
            return msg.get("result", {})


def find_browser() -> str:
    for p in EDGE_CANDIDATES:
        if Path(p).exists():
            return p
    raise SystemExit("找不到 Edge/Chrome；这个工具需要 Chromium 系浏览器")


def capture(url: str, out_png: Path, width: int, height: int) -> str:
    """用 CDP 抓渲染器输出（不是抓屏），返回引擎版本串。"""
    exe = find_browser()
    port = 9411
    profile = Path(tempfile.gettempdir()) / "mra_corner_measure_profile"
    proc = subprocess.Popen(
        [exe, f"--user-data-dir={profile}", f"--remote-debugging-port={port}",
         "--no-first-run", "--no-default-browser-check", "--disable-extensions",
         f"--window-size={width},{height}", f"--app={url}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    target = None
    try:
        for _ in range(60):
            time.sleep(0.4)
            try:
                data = json.loads(urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/json/list", timeout=2).read())
            except Exception:
                continue
            for t in data:
                if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                    target = t
                    break
            if target:
                break
        if not target:
            raise RuntimeError("没等到调试目标：浏览器可能起不来")

        sock = _ws_connect("127.0.0.1", port, target["webSocketDebuggerUrl"].split(str(port))[1])
        try:
            _call(sock, 1, "Page.enable")
            _call(sock, 2, "Emulation.setDeviceMetricsOverride",
                  {"width": width, "height": height, "deviceScaleFactor": SCALE, "mobile": False})
            time.sleep(0.8)
            res = _call(sock, 3, "Page.captureScreenshot",
                        {"format": "png", "captureBeyondViewport": True, "fromSurface": True})
            out_png.write_bytes(base64.b64decode(res["data"]))
            return _call(sock, 4, "Browser.getVersion").get("product", "unknown")
        finally:
            try:
                sock.close()
            except Exception:
                pass
    finally:
        proc.terminate()


# ---------------------------------------------------------------- 轮廓提取
def _load_gray(path: Path):
    from PIL import Image
    return Image.open(path).convert("L")


def find_boxes(img, count: int):
    """粗定位每个盒子的 (top, left)（设备像素）；随后由 refine_origin 精修。"""
    w, h = img.size
    px = img.load()
    dark_rows = []
    for y in range(h):
        n = 0
        for x in range(0, w, 4):
            if px[x, y] < 128:
                n += 1
        dark_rows.append(n)
    bands, start = [], None
    for y, n in enumerate(dark_rows):
        if n > 30 and start is None:
            start = y
        elif n <= 30 and start is not None:
            if y - start > 30:
                bands.append((start, y - 1))
            start = None
    if start is not None and len(dark_rows) - start > 30:
        bands.append((start, len(dark_rows) - 1))
    if len(bands) != count:
        raise RuntimeError(f"探针页上应有 {count} 个盒子，实际识别到 {len(bands)} 个")
    out = []
    for (y0, y1) in bands:
        left = None
        for x in range(w):
            n = sum(1 for y in range(y0, y1 + 1, 2) if px[x, y] < 128)
            if n > 5:
                left = x
                break
        out.append((y0, left))
    return out


def _edge_x(px, y, x_from, x_to):
    """一行里从左往右找第一个 50% 覆盖点，返回设备坐标（亚像素）。"""
    for x in range(max(1, x_from), x_to):
        cur = px[x, y]
        if cur < 128:
            prev = px[x - 1, y]
            return (x - 1) + (prev - 127.5) / (prev - cur) if prev != cur else float(x)
    return None


def _edge_y(px, x, y_from, y_to):
    """一列里从上往下找第一个 50% 覆盖点，返回设备坐标（亚像素）。"""
    for y in range(max(1, y_from), y_to):
        cur = px[x, y]
        if cur < 128:
            prev = px[x, y - 1]
            return (y - 1) + (prev - 127.5) / (prev - cur) if prev != cur else float(y)
    return None


def refine_origin(img, top: int, left: int, R: float):
    """用直边段做亚像素定位，返回盒子的真实左缘与上缘（设备坐标，浮点）。

    为什么必须精修：整数定位有 ±1 设备像素误差，而面积是 ∫x dy，一个像素的对位偏差
    就能让 R=20 的面积偏 20% 以上（实测过：面积比 1.03 / 0.84 的假差异全是它造成的）。
    直边段有上百个采样点，取中位数可把误差压到 0.1 设备像素内。
    """
    px = img.load()
    w, h = img.size
    base = int(R * SCALE)
    xs = []
    for y in range(int(top + base + 4), min(h, int(top + base + 4 + 30 * SCALE))):
        e = _edge_x(px, y, left - 3, min(w, left + base + 4))
        if e is not None:
            xs.append(e)
    if not xs:
        raise RuntimeError("左边直边段取不到边界，检查探针布局")
    xs.sort()
    ys = []
    for x in range(int(left + base + 4), min(w, int(left + base + 4 + 30 * SCALE))):
        e = _edge_y(px, x, top - 3, top + base + 4)
        if e is not None:
            ys.append(e)
    if not ys:
        raise RuntimeError("上边直边段取不到边界，检查探针布局")
    ys.sort()
    return xs[len(xs) // 2], ys[len(ys) // 2]


def measure_box(img, top: int, left: int, R: float):
    """量一个盒子的拐角：返回 (逐行内缩 CSS px, 角部切面积 CSS px², 纵向偏移)。

    面积用**轮廓积分**而不是数白像素：数像素要把窗口对齐到整数像素，±1 像素的对位
    误差就能让面积偏 20%；积分用亚像素边界，对窗口对位不敏感。
    """
    px = img.load()
    w, h = img.size
    left_f, top_f = refine_origin(img, top, left, R)
    n = int((R + 2) * SCALE)
    prof = []
    for i in range(n):
        y = top + i
        if y >= h:
            break
        e = _edge_x(px, y, int(left_f) - 3, min(w, int(left_f) + int(R * SCALE) + 6))
        prof.append(None if e is None else (e - left_f) / SCALE)

    area = 0.0
    for i, x in enumerate(prof):
        if x is None:
            continue
        y_css = (top + i - top_f) / SCALE
        if 0.0 <= y_css < R:
            area += max(0.0, x) / SCALE     # 每行贡献 x·dy，dy = 1/SCALE CSS
    return prof, area, (top - top_f) / SCALE


# ---------------------------------------------------------------- 主流程
def main() -> int:
    ap = argparse.ArgumentParser(description="选卡圆角几何验收（CSS 渲染 vs 理论超椭圆）")
    ap.add_argument("--keep", action="store_true", help="保留探针页与截图")
    args = ap.parse_args()

    work = Path(tempfile.mkdtemp(prefix="mra_corner_"))
    probe = work / "probe.html"
    shot = work / "probe.png"
    build_probe(probe)

    width = BOX_W + 2 * PAD
    height = int((BOX_H + 12) * len(CASES) + 2 * PAD)
    product = capture(probe.as_uri(), shot, width, height)
    print(f"引擎：{product}")
    print(f"坐标系：CSS px；截图 DPR={SCALE} → 1 CSS px = {SCALE} 设备像素")
    print(f"阈值：轮廓偏差 < {MAX_DEV_PX}px；面积比 1±{AREA_RATIO:.2f}（只判 R160）\n")

    img = _load_gray(shot)
    boxes = find_boxes(img, len(CASES))

    print(f"{'样本':26s}{'面积 实测/理论':>18s}{'比值':>8s}{'轮廓偏差':>10s}{'指数':>8s}  判定")
    failures = []
    for (name, _shape, R, exp, check_area), (top, left) in zip(CASES, boxes):
        prof, got_area, dy0 = measure_box(img, top, left, R)
        want_area = se_area(R, exp)

        dev = 0.0
        best = (1e9, None, None)          # (|x−y|, x, y)：45° 穿越点
        for i, x in enumerate(prof):
            if x is None:
                continue
            y = i / SCALE + dy0
            if y < 0.5 or y > R:
                continue
            g = se_dxdy(R, exp, y)
            dev = max(dev, abs(x - se_x(R, exp, y)) / math.sqrt(1.0 + g * g))
            if 0 < x < R and abs(x - y) < best[0]:
                best = (abs(x - y), x, y)

        # 指数：用 45° 穿越点反解（那里对指数最敏感）。u=(R−x)/R，u^n=0.5
        n_fit = None
        if best[1] is not None:
            u = 1.0 - best[1] / R
            if 0 < u < 1:
                n_fit = math.log(0.5) / math.log(u)

        ratio = got_area / want_area if want_area else 0.0
        ok = dev < MAX_DEV_PX and (abs(ratio - 1) <= AREA_RATIO if check_area else True)
        if not ok:
            failures.append(name)
        ratio_s = f"{ratio:8.2f}" if check_area else f"{'—':>8s}"
        print(f"{name:26s}{got_area:8.1f} /{want_area:7.1f}{ratio_s}{dev:9.2f}px"
              f"{(f'{n_fit:.2f}' if n_fit else '--'):>8s}  {'PASS' if ok else 'FAIL'}")

    print()
    if failures:
        print("验收不通过：" + "、".join(failures))
        print("判读：先看面积比（整体变软/变方），再看轮廓偏差（局部漂移），"
              "最后看指数是否仍为 4——指数变了说明浏览器改了实现，"
              "js/gallery-shape.js 的 EXPONENT 必须跟着改。")
        code = 1
    else:
        print("验收通过：形状是指数 4 的超椭圆，轮廓与面积都在阈值内。")
        code = 0

    if args.keep:
        print(f"\n探针页：{probe}\n截图：{shot}")
    else:
        # 连目录一起删：只删文件会每跑一次留下一个空的 mra_corner_* 目录
        import shutil
        shutil.rmtree(work, ignore_errors=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
