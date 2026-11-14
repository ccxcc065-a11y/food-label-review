"""Print HTML to PDF through a local Chrome/Chromium DevTools session.

CLI ``--print-to-pdf`` hangs on some Linux/headless Chrome 156 builds after the
process starts. Opening the same HTML over CDP and calling Page.printToPDF
returns. This module does not change the published pack.
"""
from __future__ import annotations

import base64
import json
import os
import signal
import socket
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request


def _ws_connect(url, timeout):
    parsed = urllib.parse.urlparse(url)
    host, port = parsed.hostname, parsed.port or 80
    path = parsed.path + (("?" + parsed.query) if parsed.query else "")
    sock = socket.create_connection((host, port), timeout=timeout)
    sock.settimeout(timeout)
    key = base64.b64encode(os.urandom(16)).decode()
    sock.sendall((
        "GET %s HTTP/1.1\r\nHost: %s:%s\r\nUpgrade: websocket\r\n"
        "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
        "Sec-WebSocket-Version: 13\r\n\r\n" % (path, host, port, key)
    ).encode())
    leftover = b""
    while b"\r\n\r\n" not in leftover:
        chunk = sock.recv(4096)
        if not chunk:
            raise RuntimeError("Chrome DevTools 握手失败")
        leftover += chunk
    return sock


def _ws_send(sock, payload):
    raw = json.dumps(payload).encode()
    mask = os.urandom(4)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(raw))
    header = bytearray([0x81])
    length = len(raw)
    if length < 126:
        header.append(0x80 | length)
    elif length < 65536:
        header.append(0x80 | 126)
        header.extend(length.to_bytes(2, "big"))
    else:
        header.append(0x80 | 127)
        header.extend(length.to_bytes(8, "big"))
    sock.sendall(header + mask + masked)


def _ws_recv(sock):
    def recvn(n):
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise RuntimeError("Chrome DevTools 连接已关闭")
            buf += chunk
        return buf

    data = b""
    while True:
        first, second = recvn(2)
        opcode = first & 0x0F
        length = second & 0x7F
        if length == 126:
            length = int.from_bytes(recvn(2), "big")
        elif length == 127:
            length = int.from_bytes(recvn(8), "big")
        if second & 0x80:
            mask = recvn(4)
            chunk = bytes(b ^ mask[i % 4] for i, b in enumerate(recvn(length)))
        else:
            chunk = recvn(length)
        if opcode == 0x8:
            raise RuntimeError("Chrome DevTools 连接关闭")
        if opcode == 0x9:  # ping
            # unmasked pong is fine from client? RFC requires client mask.
            _ws_send_pong(sock, chunk)
            continue
        if opcode in (0x1, 0x2, 0x0):
            data += chunk
            if first & 0x80:
                return data


def _ws_send_pong(sock, payload):
    mask = os.urandom(4)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    header = bytearray([0x8A, 0x80 | len(payload)]) if len(payload) < 126 else None
    if header is None:
        raise RuntimeError("unexpected ping size")
    sock.sendall(header + mask + masked)


def _cdp_call(sock, mid, method, params=None, timeout_at=None):
    _ws_send(sock, {"id": mid, "method": method, **({"params": params} if params else {})})
    while True:
        if timeout_at is not None and time.time() > timeout_at:
            raise TimeoutError("Chrome PDF 渲染超时")
        message = json.loads(_ws_recv(sock))
        if message.get("id") == mid:
            if message.get("error"):
                raise RuntimeError("Chrome DevTools %s 失败：%s" % (method, message["error"]))
            return message.get("result") or {}


def _kill(proc, profile):
    if proc is not None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            proc.wait(timeout=2)
        except Exception:
            pass
    try:
        out = subprocess.check_output(["ps", "-eo", "pid,cmd"], text=True)
        for line in out.splitlines():
            if profile and profile in line:
                try:
                    os.kill(int(line.split()[0]), signal.SIGKILL)
                except Exception:
                    pass
    except Exception:
        pass


def _wait_json(url, timeout):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                return json.loads(response.read())
        except Exception as exc:
            last = exc
            time.sleep(0.1)
    raise RuntimeError("无法连接 Chrome DevTools（%s）" % last)


def print_html(chrome, html_text, dest, timeout=60):
    """Render html_text to dest using Chrome DevTools Page.printToPDF."""
    profile = tempfile.mkdtemp(prefix="label-pdf-")
    source = os.path.join(profile, "report.html")
    with open(source, "w", encoding="utf-8") as handle:
        handle.write(html_text)
    sock_holder = socket.socket(); sock_holder.bind(("127.0.0.1", 0))
    port = sock_holder.getsockname()[1]
    sock_holder.close()
    env = dict(os.environ)
    env.pop("DISPLAY", None)
    env.pop("WAYLAND_DISPLAY", None)
    cmd = [
        chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
        "--disable-dev-shm-usage", "--no-first-run", "--disable-background-networking",
        "--disable-extensions", "--no-default-browser-check",
        "--remote-debugging-address=127.0.0.1",
        "--remote-debugging-port=%d" % port,
        "--user-data-dir=" + os.path.join(profile, "chrome"),
        "file://" + source,
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            env=env, start_new_session=True)
    sock = None
    try:
        _wait_json("http://127.0.0.1:%d/json/version" % port, min(10, timeout))
        tabs = _wait_json("http://127.0.0.1:%d/json/list" % port, min(10, timeout))
        page = next((tab for tab in tabs if tab.get("type") == "page"
                     and tab.get("webSocketDebuggerUrl")
                     and "file://" in str(tab.get("url") or "")), None)
        if not page:
            page = next((tab for tab in tabs if tab.get("type") == "page"
                         and tab.get("webSocketDebuggerUrl")), None)
        if not page:
            raise RuntimeError("Chrome 未打开报告页")
        sock = _ws_connect(page["webSocketDebuggerUrl"], timeout)
        deadline = time.time() + timeout
        _cdp_call(sock, 1, "Page.enable", timeout_at=deadline)
        result = _cdp_call(sock, 2, "Page.printToPDF", {
            "printBackground": True,
            "preferCSSPageSize": True,
        }, timeout_at=deadline)
        data = result.get("data")
        if not data:
            raise RuntimeError("Chrome 未返回 PDF 数据")
        raw = base64.b64decode(data)
        if len(raw) < 100 or not raw.startswith(b"%PDF"):
            raise RuntimeError("Chrome 返回的 PDF 无效")
        with open(dest, "wb") as handle:
            handle.write(raw)
        return dest
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass
        _kill(proc, profile)
        try:
            import shutil
            shutil.rmtree(profile, ignore_errors=True)
        except Exception:
            pass
