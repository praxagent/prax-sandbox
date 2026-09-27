"""CDP (Chrome DevTools Protocol) service — talks to the sandbox Chrome instance.

This connects to the same headless Chrome the user sees via the TeamWork
browser panel.  Prax uses this for reading page content, taking screenshots,
and controlling the browser when the user hasn't taken over.

Uses only stdlib (urllib + socket) so no extra dependencies are needed.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import struct
import time
import urllib.request
from base64 import b64encode
from threading import Lock

logger = logging.getLogger(__name__)

# Default to the loopback-published port: the usual consumer is a host process
# next to the container (compose publishes 9223 on 127.0.0.1).  Compose-internal
# consumers (a dockerized harness on the same network) set SANDBOX_HOST=sandbox
# explicitly in their compose environment.
CDP_HOST = os.getenv("SANDBOX_HOST", "localhost")
CDP_PORT = int(os.getenv("CDP_PORT", "9223"))

_lock = Lock()
_msg_counter = 0


def _remote():
    """Return (base_url, token, tls_verify) when a control daemon is configured,
    else None. In remote mode CDP is reached only through the authed daemon
    proxy (raw :9223 is never network-exposed)."""
    try:
        from prax_sandbox import control_plane
        cfg = control_plane._cfg()
        if cfg and (cfg.daemon_url or "").strip():
            return cfg.daemon_url.rstrip("/"), cfg.daemon_token, cfg.tls_verify
    except Exception:
        pass
    return None


def _ssl_context(tls_verify):
    import ssl
    if tls_verify is False:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    if isinstance(tls_verify, str):
        return ssl.create_default_context(cafile=tls_verify)
    return ssl.create_default_context()


# ---------------------------------------------------------------------------
# Minimal WebSocket client (RFC 6455) — just enough for CDP request/response
# ---------------------------------------------------------------------------

def _ws_connect(url: str, timeout: float = 10) -> socket.socket:
    """Open a WebSocket connection using raw sockets (ws:// or wss://)."""
    if url.startswith("wss://"):
        secure, rest = True, url[6:]
    elif url.startswith("ws://"):
        secure, rest = False, url[5:]
    else:
        raise ValueError(f"unexpected websocket url: {url[:24]}")
    host_port, _, tail = rest.partition("/")
    path = "/" + tail
    if ":" in host_port:
        host, port_s = host_port.split(":", 1)
        port = int(port_s)
    else:
        host, port = host_port, (443 if secure else 80)

    remote = _remote()
    sock = socket.create_connection((host, port), timeout=timeout)
    if secure:
        sock = _ssl_context(remote[2] if remote else True).wrap_socket(sock, server_hostname=host)

    # Local mode pins Host to loopback (the in-container proxy expects it);
    # remote mode uses the daemon host and carries the bearer token.
    key = b64encode(os.urandom(16)).decode()
    lines = [
        f"GET {path} HTTP/1.1",
        f"Host: {host_port if remote else f'127.0.0.1:{CDP_PORT}'}",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Key: {key}",
        "Sec-WebSocket-Version: 13",
        # Chrome is launched with --remote-allow-origins=http://127.0.0.1:9222;
        # present it so the local direct-to-Chrome path is allowed. (Remote mode
        # hits the daemon, which gates on the bearer, not Origin.)
        "Origin: http://127.0.0.1:9222",
    ]
    if remote and remote[1]:
        lines.append(f"Authorization: Bearer {remote[1]}")
    sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())

    # Read response headers
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("WebSocket handshake failed — no response")
        buf += chunk

    if b"101" not in buf.split(b"\r\n")[0]:
        raise ConnectionError(f"WebSocket handshake rejected: {buf[:200]}")

    return sock


def _ws_send(sock: socket.socket, data: str) -> None:
    """Send a WebSocket text frame (masked, as client)."""
    payload = data.encode("utf-8")
    frame = bytearray()
    frame.append(0x81)  # FIN + text opcode

    length = len(payload)
    if length < 126:
        frame.append(0x80 | length)  # MASK bit set
    elif length < 65536:
        frame.append(0x80 | 126)
        frame.extend(struct.pack("!H", length))
    else:
        frame.append(0x80 | 127)
        frame.extend(struct.pack("!Q", length))

    mask = os.urandom(4)
    frame.extend(mask)
    frame.extend(bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))
    sock.sendall(frame)


def _ws_recv(sock: socket.socket) -> str:
    """Receive a WebSocket text frame."""
    def _read_exact(n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("WebSocket connection closed")
            buf += chunk
        return buf

    header = _read_exact(2)
    opcode = header[0] & 0x0F
    masked = bool(header[1] & 0x80)
    length = header[1] & 0x7F

    if length == 126:
        length = struct.unpack("!H", _read_exact(2))[0]
    elif length == 127:
        length = struct.unpack("!Q", _read_exact(8))[0]

    if masked:
        mask = _read_exact(4)
        data = _read_exact(length)
        data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    else:
        data = _read_exact(length)

    if opcode == 0x08:  # close
        raise ConnectionError("WebSocket closed by server")
    if opcode == 0x01:  # text
        return data.decode("utf-8")
    # For ping/pong/continuation, just return empty and let caller retry
    return ""


# ---------------------------------------------------------------------------
# CDP protocol helpers
# ---------------------------------------------------------------------------

def _cdp_http(path: str, timeout: float = 5) -> dict | list | None:
    """Make an HTTP request for CDP targets — via the daemon proxy if remote."""
    remote = _remote()
    if remote:
        base, token, tls_verify = remote
        req = urllib.request.Request(
            f"{base}/v1/cdp{path}", headers={"Authorization": f"Bearer {token or ''}"})
        ctx = _ssl_context(tls_verify) if base.startswith("https") else None
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                return json.loads(resp.read())
        except Exception as e:
            logger.warning("CDP HTTP (daemon) failed (%s): %s", path, e)
            return None
    url = f"http://{CDP_HOST}:{CDP_PORT}{path}"
    req = urllib.request.Request(url, headers={"Host": f"127.0.0.1:{CDP_PORT}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception as e:
        logger.warning("CDP HTTP failed (%s): %s", path, e)
        return None


def _discover_ws_url() -> str | None:
    """Find the WebSocket debugger URL for the most relevant page target.

    Picks the *last* page target in the list — Chrome appends new tabs/popups
    at the end, so this naturally targets auth popups and modal windows that
    open in a new tab.  When the popup closes, the next call falls back to
    the original tab.
    """
    targets = _cdp_http("/json")
    if not targets or not isinstance(targets, list):
        return None
    pages = [t for t in targets if t.get("type") == "page"]
    if not pages:
        return None
    # Prefer the last (most recently opened) page target
    page = pages[-1]
    ws_url: str = page["webSocketDebuggerUrl"]
    if _remote():
        # The daemon already rewrote webSocketDebuggerUrl to its authed
        # wss://<daemon>/v1/cdp/ws/... endpoint — use it as-is.
        return ws_url
    ws_url = ws_url.replace("localhost", CDP_HOST)
    ws_url = ws_url.replace("127.0.0.1", CDP_HOST)
    ws_url = ws_url.replace(":9222/", f":{CDP_PORT}/")
    return ws_url


def _send_cdp(method: str, params: dict | None = None, timeout: float = 10) -> dict:
    """Send a CDP command and wait for the response."""
    global _msg_counter

    ws_url = _discover_ws_url()
    if not ws_url:
        return {"error": "Chrome not reachable — no page target found"}

    _msg_counter += 1
    msg_id = _msg_counter
    payload: dict = {"id": msg_id, "method": method}
    if params:
        payload["params"] = params

    try:
        sock = _ws_connect(ws_url, timeout=timeout)
        sock.settimeout(timeout)
        _ws_send(sock, json.dumps(payload))

        deadline = time.time() + timeout
        while time.time() < deadline:
            raw = _ws_recv(sock)
            if not raw:
                continue
            data = json.loads(raw)
            if data.get("id") == msg_id:
                sock.close()
                if "error" in data:
                    return {"error": data["error"].get("message", str(data["error"]))}
                return data.get("result", {})
        sock.close()
        return {"error": "CDP command timed out"}
    except Exception as e:
        return {"error": f"CDP command failed: {e}"}


# ---------------------------------------------------------------------------
# Public API — called by Prax tools
# ---------------------------------------------------------------------------

def is_available() -> bool:
    """Check if Chrome is reachable."""
    info = _cdp_http("/json/version")
    return info is not None and isinstance(info, dict)


def get_page_text(max_length: int = 30_000) -> dict:
    """Get the visible text content of the current page."""
    with _lock:
        result = _send_cdp("Runtime.evaluate", {
            "expression": "document.body?.innerText || ''",
            "returnByValue": True,
        })
    if "error" in result:
        return result

    text = result.get("result", {}).get("value", "")
    if len(text) > max_length:
        text = text[:max_length] + "\n\n[Content truncated]"

    url_result = _send_cdp("Runtime.evaluate", {
        "expression": "document.location.href",
        "returnByValue": True,
    })
    title_result = _send_cdp("Runtime.evaluate", {
        "expression": "document.title",
        "returnByValue": True,
    })
    url = url_result.get("result", {}).get("value", "unknown")
    title = title_result.get("result", {}).get("value", "Untitled")

    return {"text": text, "url": url, "title": title}


def get_page_url() -> dict:
    """Get the current page URL and title."""
    result = _send_cdp("Runtime.evaluate", {
        "expression": "JSON.stringify({url: location.href, title: document.title})",
        "returnByValue": True,
    })
    if "error" in result:
        return result
    try:
        return json.loads(result.get("result", {}).get("value", "{}"))
    except (json.JSONDecodeError, TypeError):
        return {"url": "unknown", "title": "unknown"}


def navigate(url: str) -> dict:
    """Navigate to a URL and wait for the page to finish loading.

    Uses DOM readyState polling instead of a fixed sleep so fast pages
    return immediately and slow pages (HN discussion threads, JS-heavy
    sites) get enough time to render.
    """
    with _lock:
        result = _send_cdp("Page.navigate", {"url": url}, timeout=15)
    if "error" in result:
        return result

    # Poll readyState until "complete" (max ~8s).
    deadline = time.time() + 8
    while time.time() < deadline:
        time.sleep(0.5)
        rs = _send_cdp("Runtime.evaluate", {
            "expression": "document.readyState",
            "returnByValue": True,
        })
        state = rs.get("result", {}).get("value", "")
        if state == "complete":
            break

    # Extra settle time for JS-rendered content.
    time.sleep(1)
    return get_page_text(max_length=5000)


def screenshot() -> dict:
    """Take a screenshot and return base64 JPEG data."""
    with _lock:
        result = _send_cdp("Page.captureScreenshot", {
            "format": "jpeg",
            "quality": 80,
        })
    if "error" in result:
        return result
    b64 = result.get("data", "")
    if not b64:
        return {"error": "No screenshot data returned"}

    # Save to temp file so tools can reference it
    import tempfile
    path = os.path.join(tempfile.gettempdir(), f"cdp_screenshot_{int(time.time())}.jpg")
    import base64
    with open(path, "wb") as f:
        f.write(base64.b64decode(b64))
    return {"path": path, "format": "jpeg"}


def click_element(selector: str) -> dict:
    """Click an element by CSS selector."""
    with _lock:
        result = _send_cdp("Runtime.evaluate", {
            "expression": f"""
                (() => {{
                    const el = document.querySelector({json.dumps(selector)});
                    if (!el) return JSON.stringify({{error: 'Element not found: {selector}'}});
                    const rect = el.getBoundingClientRect();
                    return JSON.stringify({{
                        x: Math.round(rect.x + rect.width / 2),
                        y: Math.round(rect.y + rect.height / 2),
                    }});
                }})()
            """,
            "returnByValue": True,
        })
    if "error" in result:
        return result

    try:
        pos = json.loads(result.get("result", {}).get("value", "{}"))
    except (json.JSONDecodeError, TypeError):
        return {"error": "Failed to parse element position"}
    if "error" in pos:
        return pos
    if "x" not in pos or "y" not in pos:
        return {"error": f"Element not found or not visible for selector: {selector}"}

    return click_at(pos["x"], pos["y"])


def click_text(text: str) -> dict:
    """Click an element by its visible text content.

    Searches all clickable elements (a, button, input, [role=button], etc.)
    for one whose visible text contains the given string (case-insensitive).
    """
    js = f"""
        (() => {{
            const target = {json.dumps(text)}.toLowerCase();
            // Walk ALL elements — modern SPAs use custom tags, roles, etc.
            const all = document.querySelectorAll('*');
            let best = null;
            let bestLen = Infinity;
            for (const el of all) {{
                // Skip non-leaf containers with too much text (body, main, etc.)
                const t = (el.innerText || el.textContent || el.value || '').trim();
                if (!t || t.length > 500) continue;
                if (t.toLowerCase().includes(target) && t.length < bestLen) {{
                    const rect = el.getBoundingClientRect();
                    // Must be visible and have size
                    if (rect.width > 0 && rect.height > 0 && rect.bottom > 0 && rect.right > 0) {{
                        best = {{ x: Math.round(rect.x + rect.width / 2), y: Math.round(rect.y + rect.height / 2), matched: t.substring(0, 80) }};
                        bestLen = t.length;
                    }}
                }}
            }}
            if (!best) return JSON.stringify({{ error: 'No visible element with text: ' + {json.dumps(text)} }});
            return JSON.stringify(best);
        }})()
    """
    with _lock:
        result = _send_cdp("Runtime.evaluate", {
            "expression": js,
            "returnByValue": True,
        })
    if "error" in result:
        return result

    try:
        pos = json.loads(result.get("result", {}).get("value", "{}"))
    except (json.JSONDecodeError, TypeError):
        return {"error": "Failed to parse element position"}
    if "error" in pos:
        return pos
    if "x" not in pos or "y" not in pos:
        return {"error": f"No clickable element found with text: {text}"}

    matched = pos.get("matched", text)
    click_result = click_at(pos["x"], pos["y"])
    if "error" in click_result:
        return click_result
    return {"status": f"Clicked '{matched}' at ({pos['x']}, {pos['y']})"}


def click_at(x: int, y: int) -> dict:
    """Click at absolute coordinates."""
    with _lock:
        _send_cdp("Input.dispatchMouseEvent", {
            "type": "mousePressed", "x": x, "y": y,
            "button": "left", "clickCount": 1,
        })
        time.sleep(0.05)
        _send_cdp("Input.dispatchMouseEvent", {
            "type": "mouseReleased", "x": x, "y": y,
            "button": "left", "clickCount": 1,
        })
    time.sleep(0.5)
    return {"status": f"Clicked at ({x}, {y})"}


def type_text(text: str) -> dict:
    """Type text into the focused element.

    Single-line text goes key by key, so pages that listen for keystrokes
    (search-as-you-type, autocomplete) see them. Text with a newline goes in
    as one insertion instead: key by key, a code editor re-indents after every
    Enter and auto-closes every bracket, so pasted code came out mangled — and
    a bare "\n" char event did not break the line at all, leaving a whole
    program on line 1.
    """
    if "\n" in text:
        return insert_text(text)
    with _lock:
        for ch in text:
            _send_cdp("Input.dispatchKeyEvent", {
                "type": "keyDown", "key": ch, "text": "",
            })
            _send_cdp("Input.dispatchKeyEvent", {
                "type": "char", "key": ch, "text": ch,
            })
            _send_cdp("Input.dispatchKeyEvent", {
                "type": "keyUp", "key": ch, "text": "",
            })
            time.sleep(0.03)
    return {"status": f"Typed {len(text)} characters"}


def insert_text(text: str) -> dict:
    """Paste *text* at the caret, exactly as a person pasting would.

    Code editors (Monaco, CodeMirror) format TYPED input — auto-indent after
    each newline, auto-close brackets — and take a real paste verbatim.
    Measured on a live Monaco editor (LeetCode):

    - key-by-key typing and ``Input.insertText`` are both treated as typing:
      every line indented further than the last, a stray "}" appended;
    - a scripted ``paste`` event is cancelled by the editor and inserts nothing;
    - the clipboard plus the browser's own paste command reproduces the text
      exactly, and needs no clipboard permission granted to the page.

    So: put the text on the browser clipboard (a user-gesture write, allowed
    for the focused page), then send Ctrl+V as the native "paste" command.
    If the clipboard write is refused, fall back to ``Input.insertText``,
    which is right for plain inputs. The clipboard keeps the pasted text.
    """
    write = (
        f"navigator.clipboard.writeText({json.dumps(text)})"
        ".then(() => 'ok', e => 'refused: ' + e)"
    )
    with _lock:
        written = _send_cdp("Runtime.evaluate", {
            "expression": write, "awaitPromise": True,
            "userGesture": True, "returnByValue": True,
        })
        if isinstance(written, dict) and written.get("result", {}).get("value") == "ok":
            paste = {"type": "rawKeyDown", "key": "v", "code": "KeyV",
                     "windowsVirtualKeyCode": 86, "modifiers": 2, "commands": ["paste"]}
            _send_cdp("Input.dispatchKeyEvent", paste)
            _send_cdp("Input.dispatchKeyEvent", {"type": "keyUp", "key": "v", "code": "KeyV",
                                                 "windowsVirtualKeyCode": 86, "modifiers": 2})
            return {"status": f"Pasted {len(text)} characters"}
        result = _send_cdp("Input.insertText", {"text": text})
    if isinstance(result, dict) and result.get("error"):
        return {"error": f"insert failed: {result['error']}"}
    return {"status": f"Inserted {len(text)} characters"}


# Named keys: (DOM code, Windows virtual key code).
_NAMED_KEYS = {
    "Enter": ("Enter", 13), "Tab": ("Tab", 9), "Escape": ("Escape", 27),
    "Backspace": ("Backspace", 8), "Delete": ("Delete", 46), "Insert": ("Insert", 45),
    "Home": ("Home", 36), "End": ("End", 35), "PageUp": ("PageUp", 33),
    "PageDown": ("PageDown", 34), "ArrowUp": ("ArrowUp", 38),
    "ArrowDown": ("ArrowDown", 40), "ArrowLeft": ("ArrowLeft", 37),
    "ArrowRight": ("ArrowRight", 39), " ": ("Space", 32), "Space": ("Space", 32),
    **{f"F{i}": (f"F{i}", 111 + i) for i in range(1, 13)},
}
_KEY_ALIASES = {
    "esc": "Escape", "return": "Enter", "del": "Delete", "space": "Space",
    "up": "ArrowUp", "down": "ArrowDown", "left": "ArrowLeft", "right": "ArrowRight",
    "pageup": "PageUp", "pagedown": "PageDown",
}
# CDP modifier bits, and each modifier's own key.
_MODIFIERS = {
    "alt": (1, "Alt", "AltLeft", 18), "option": (1, "Alt", "AltLeft", 18),
    "control": (2, "Control", "ControlLeft", 17), "ctrl": (2, "Control", "ControlLeft", 17),
    "meta": (4, "Meta", "MetaLeft", 91), "cmd": (4, "Meta", "MetaLeft", 91),
    "command": (4, "Meta", "MetaLeft", 91),
    "shift": (8, "Shift", "ShiftLeft", 16),
}


def _parse_key(spec: str) -> tuple[list[tuple], str, str, int, str] | str:
    """``"Control+Shift+End"`` -> (modifiers, key, code, keyCode, text), or an error."""
    parts = spec.split("+")
    if len(parts) > 1 and parts[-1] == "":        # "Control++" presses "+"
        parts = parts[:-2] + ["+"]
    *mods, key = [p.strip() if p.strip() else p for p in parts]
    modifiers = []
    for m in mods:
        if m.lower() not in _MODIFIERS:
            return f"unknown modifier {m!r} in {spec!r} (use Control, Shift, Alt, Meta)"
        modifiers.append(_MODIFIERS[m.lower()])
    key = _KEY_ALIASES.get(key.lower(), key)
    if key in _NAMED_KEYS:
        code, vk = _NAMED_KEYS[key]
        text = "\r" if key == "Enter" else (" " if code == "Space" else "")
        return modifiers, (" " if code == "Space" else key), code, vk, text
    if len(key) == 1:
        shifted = any(m[0] == 8 for m in modifiers)
        if key.isalpha():
            code, vk = f"Key{key.upper()}", ord(key.upper())
            key = key.upper() if shifted else key.lower()
        elif key.isdigit():
            code, vk = f"Digit{key}", ord(key)
        else:
            code, vk = "", 0
        return modifiers, key, code, vk, key
    return (f"unknown key {key!r} — use a single character or one of: "
            + ", ".join(k for k in _NAMED_KEYS if k != " "))


def press_key(key: str) -> dict:
    """Press a key or a shortcut: "Enter", "Tab", "Control+a", "Control+Shift+End".

    Modifiers are sent as real modifier state. Before, "Control+a" went out as
    a single key literally named "Control+a" with no modifiers, which pages
    ignore — while the tool still reported "Pressed Control+a", so an agent
    believed it had selected everything and typed a second copy after the first.
    """
    parsed = _parse_key(key)
    if isinstance(parsed, str):
        return {"error": parsed}
    modifiers, name, code, vk, text = parsed
    bits = 0
    # A shortcut (any modifier but Shift) must not also type its character.
    typing = bool(text) and not any(m[0] in (1, 2, 4) for m in modifiers)
    with _lock:
        for bit, mkey, mcode, mvk in modifiers:
            bits |= bit
            _send_cdp("Input.dispatchKeyEvent", {
                "type": "rawKeyDown", "key": mkey, "code": mcode,
                "windowsVirtualKeyCode": mvk, "modifiers": bits,
            })
        down = {"type": "keyDown" if typing else "rawKeyDown", "key": name, "code": code,
                "windowsVirtualKeyCode": vk, "modifiers": bits}
        if typing:
            down["text"] = text
        _send_cdp("Input.dispatchKeyEvent", down)
        _send_cdp("Input.dispatchKeyEvent", {
            "type": "keyUp", "key": name, "code": code,
            "windowsVirtualKeyCode": vk, "modifiers": bits,
        })
        for bit, mkey, mcode, mvk in reversed(modifiers):
            bits &= ~bit
            _send_cdp("Input.dispatchKeyEvent", {
                "type": "keyUp", "key": mkey, "code": mcode,
                "windowsVirtualKeyCode": mvk, "modifiers": bits,
            })
    return {"status": f"Pressed {key}"}


def scroll_page(direction: str = "down", amount: int = 300) -> dict:
    """Scroll the page. direction: 'up' or 'down'."""
    delta = amount if direction == "down" else -amount
    with _lock:
        result = _send_cdp("Input.dispatchMouseEvent", {
            "type": "mouseWheel",
            "x": 640, "y": 450,
            "deltaX": 0, "deltaY": delta,
        })
    if "error" in result:
        return result
    return {"status": f"Scrolled {direction} by {abs(delta)}px"}


def evaluate_js(expression: str) -> dict:
    """Evaluate arbitrary JavaScript in the page context."""
    with _lock:
        result = _send_cdp("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
        })
    if "error" in result:
        return result
    value = result.get("result", {}).get("value")
    return {"value": value}
