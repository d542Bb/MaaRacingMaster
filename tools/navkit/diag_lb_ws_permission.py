"""mpelb LocalBridge 权限报错复现实验（C 类最小实验）。

目的：证实 [PERMISSION_DENIED] 权限不足或路径非法 的唯一触发面是
validatePath 越界检查（路径不在 --root 范围内），且正常 root 内
open_file 不会报错。纯标准库 WebSocket 客户端，零新增依赖。

用法：先确保 mpelb 已在 26521 运行（mpe.cmd 或手动启动），然后
    .venv\\Scripts\\python.exe tools\\experiments\\v4-p3-studio\\diag_lb_ws_permission.py
"""

from pathlib import Path
import base64
import json
import os
import socket
import struct
import sys

HOST = "127.0.0.1"
PORT = 26521


def ws_connect():
    sock = socket.create_connection((HOST, PORT), timeout=5)
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
        raise RuntimeError(f"handshake failed: {header[:200]!r}")
    return sock, rest


def ws_send(sock, payload: str):
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


def summarize(msg: dict) -> str:
    path = msg.get("path", msg.get("Path", "?"))
    data = msg.get("data", msg.get("Data", {}))
    if isinstance(data, dict):
        keys = sorted(data.keys())
        brief = {}
        for k in ("file_path", "status", "message", "code", "reason", "detail", "root"):
            if k in data:
                v = data[k]
                brief[k] = v if not isinstance(v, dict) else json.dumps(v, ensure_ascii=False)
        if path == "/lte/file_list":
            files = data.get("files") or []
            brief = {"root": data.get("root"), "file_count": len(files),
                     "first_files": [f.get("file_path") for f in files[:3]]}
        return f"path={path} brief={json.dumps(brief, ensure_ascii=False)} keys={keys[:12]}"
    return f"path={path} data={str(data)[:120]}"


def read_until_response(reader, limit=80):
    for _ in range(limit):
        try:
            msg = json.loads(reader.read_text())
        except (TimeoutError, EOFError):
            return None
        path = msg.get("path", msg.get("Path", ""))
        if path in ("/error", "/lte/file_content"):
            return msg
    return None


def main():
    in_root = str(Path(__file__).resolve().parents[3] / "maaracing_assistant/plugins/treasure/resources/pipeline/treasure.json")
    out_root = r"D:\mpe-poc\workspace\pipeline\treasure.json"

    sock, rest = ws_connect()
    reader = FrameReader(sock, rest)

    print(f"[1] open_file root 内真源: {in_root}")
    ws_send(sock, json.dumps(
        {"path": "/etl/open_file", "data": {"file_path": in_root}}))
    msg = read_until_response(reader)
    print("   <-", summarize(msg) if msg else "无响应")

    print(f"[2] open_file root 外旧试验田路径: {out_root}")
    ws_send(sock, json.dumps(
        {"path": "/etl/open_file", "data": {"file_path": out_root}}))
    msg = read_until_response(reader)
    print("   <-", summarize(msg) if msg else "无响应")

    sock.close()
    print("[done]")


if __name__ == "__main__":
    sys.exit(main())
