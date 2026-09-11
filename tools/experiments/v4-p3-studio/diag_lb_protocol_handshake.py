#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LocalBridge 协议握手验证（新版 mpelb 是否满足 MPE v1.10 要求的协议版本）。

背景：MPE v1.10.0 起前端握手要求协议 1.5.0，mpelb 1.9.3 是 1.4.6 → 后端检测到
不一致后**主动退出**（现象：mpe.cmd 起来一会儿就没了）。

做法：起一个隔离端口的 mpelb，连 WS 发 `/system/handshake`，读
`/system/handshake/response` 的 protocol_version，并核对日志里没有版本不匹配错误。
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
MPELB = REPO / "tools/navkit/dev/mpelb.exe"
LOG_DIR = REPO / "tools/navkit/dev/_lb_handshake_log"
HOST = "127.0.0.1"
PORT = 26598
REQUIRED = "1.5.0"   # MPE v1.10.0 前端握手要求


def ws_connect(timeout: float = 5.0):
    sock = socket.create_connection((HOST, PORT), timeout=timeout)
    key = base64.b64encode(os.urandom(16)).decode()
    req = (
        f"GET / HTTP/1.1\r\nHost: {HOST}:{PORT}\r\n"
        "Upgrade: websocket\r\nConnection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
    )
    sock.sendall(req.encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise RuntimeError("handshake closed")
        buf += chunk
    header, rest = buf.split(b"\r\n\r\n", 1)
    if b"101" not in header.split(b"\r\n")[0]:
        raise RuntimeError(f"ws upgrade failed: {header[:200]!r}")
    return sock, rest


def ws_send(sock, payload: str) -> None:
    data = payload.encode("utf-8")
    mask = os.urandom(4)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    header = bytearray([0x81])
    n = len(data)
    if n < 126:
        header.append(0x80 | n)
    elif n < 65536:
        header.append(0x80 | 126)
        header += struct.pack("!H", n)
    else:
        header.append(0x80 | 127)
        header += struct.pack("!Q", n)
    sock.sendall(bytes(header) + mask + masked)


class FrameReader:
    def __init__(self, sock, rest: bytes):
        self.sock = sock
        self.buf = rest

    def _need(self, n: int):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise EOFError("connection closed")
            self.buf += chunk

    def read_text(self) -> str:
        self._need(2)
        opcode = self.buf[0] & 0x0F
        length = self.buf[1] & 0x7F
        idx = 2
        if length == 126:
            self._need(idx + 2)
            length = struct.unpack("!H", self.buf[idx:idx + 2])[0]
            idx += 2
        elif length == 127:
            self._need(idx + 8)
            length = struct.unpack("!Q", self.buf[idx:idx + 8])[0]
            idx += 8
        self._need(idx + length)
        payload = self.buf[idx:idx + length]
        self.buf = self.buf[idx + length:]
        if opcode == 0x8:
            raise EOFError("server close frame")
        return payload.decode("utf-8", errors="replace")


def main() -> int:
    if LOG_DIR.exists():
        shutil.rmtree(LOG_DIR, ignore_errors=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    print(f"mpelb 版本: {subprocess.run([str(MPELB), '--version'], capture_output=True, text=True).stdout.strip()}")
    print(f"前端要求协议: {REQUIRED}")

    proc = subprocess.Popen(
        [str(MPELB), "--root", str(REPO), "--port", str(PORT),
         "--log-level", "INFO", "--log-dir", str(LOG_DIR)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        # 等端口 LISTENING
        for _ in range(60):
            try:
                with socket.create_connection((HOST, PORT), timeout=0.5):
                    break
            except OSError:
                time.sleep(0.25)
        else:
            print("[FAIL] mpelb 端口未监听")
            return 1

        sock, rest = ws_connect()
        reader = FrameReader(sock, rest)
        print("\n--- 发送 /system/handshake ---")
        ws_send(sock, json.dumps({
            "path": "/system/handshake",
            "data": {"protocol_version": REQUIRED, "client": "maaracing-studio-check"},
        }, ensure_ascii=False))

        resp = None
        seen: list[str] = []
        sock.settimeout(8)
        for _ in range(60):
            try:
                msg = json.loads(reader.read_text())
            except (TimeoutError, EOFError, OSError):
                break
            path = msg.get("path") or msg.get("Path") or ""
            seen.append(path)
            if "handshake" in path:
                resp = msg
                break

        print("收到路径:", [p for p in seen if p][:8])
        if resp is None:
            print("[FAIL] 未收到 /system/handshake/response")
            return 1
        print("--- 握手响应 ---")
        print(json.dumps(resp, ensure_ascii=False, indent=2)[:800])

        data = resp.get("data") or resp.get("Data") or {}
        got = data.get("server_version") or data.get("protocol_version")
        required = data.get("required_version")
        success = data.get("success")
        ok = bool(success) and str(got) == REQUIRED and str(required) == REQUIRED
        print(f"\n协议版本: 本地={got} 要求={required} success={success} -> "
              f"{'匹配' if ok else '不匹配'}")

        # 连接是否被服务端主动断开（旧版会 close 1005 并退出）
        alive = proc.poll() is None
        print(f"mpelb 进程存活: {alive}")
        if not ok or not alive:
            print("[FAIL] 握手未通过")
            return 1
        print("[OK] 握手通过，mpelb 未主动退出")
        return 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
        # 打印日志里的版本相关行
        for log in sorted(LOG_DIR.glob("*.log")):
            lines = [l for l in log.read_text(encoding="utf-8", errors="replace").splitlines()
                     if "协议" in l or "protocol" in l.lower() or "握手" in l]
            if lines:
                print(f"\n--- {log.name} 版本相关日志 ---")
                for l in lines[:10]:
                    print("   ", l)
        shutil.rmtree(LOG_DIR, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
