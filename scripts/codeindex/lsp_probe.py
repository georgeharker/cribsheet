#!/usr/bin/env python3
"""Derisk step 1: get callers/callees for a symbol from a live LSP server.

Minimal raw JSON-RPC (stdio, Content-Length framing) client — no lsprotocol yet;
this proves the fiddly path (initialize + capability advertisement + the
workspace/configuration pull + call hierarchy) before we adopt lsprotocol and build
the warm-session subsystem. Launches pyright, opens a file, locates a symbol via
documentSymbol, then prepareCallHierarchy → incoming/outgoing calls.

    python scripts/codeindex/lsp_probe.py crib/app.py _generate_index
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import IO, Any


class Client:
    def __init__(self, cmd: list[str], root: Path) -> None:
        self.root = root
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        assert self.proc.stdin and self.proc.stdout
        self.w: IO[bytes] = self.proc.stdin
        self.r: IO[bytes] = self.proc.stdout
        self._id = 0
        self._resp: dict[int, dict] = {}
        self._ev = threading.Event()
        self._lock = threading.Lock()
        threading.Thread(target=self._reader, daemon=True).start()

    # --- transport ---------------------------------------------------------
    def _send(self, msg: dict) -> None:
        data = json.dumps(msg).encode()
        self.w.write(f"Content-Length: {len(data)}\r\n\r\n".encode() + data)
        self.w.flush()

    def _reader(self) -> None:
        f = self.r
        while True:
            # read headers
            headers = {}
            while True:
                line = f.readline()
                if not line:
                    return
                line = line.decode().strip()
                if not line:
                    break
                k, _, v = line.partition(":")
                headers[k.strip().lower()] = v.strip()
            n = int(headers.get("content-length", 0))
            body = f.read(n)
            msg = json.loads(body)
            if "id" in msg and "method" in msg:          # server → client request
                self._on_request(msg)
            elif "id" in msg:                            # response
                with self._lock:
                    self._resp[msg["id"]] = msg
                self._ev.set()
            # notifications (method, no id) ignored for the probe

    def _on_request(self, msg: dict) -> None:
        # Answer the handful of server-initiated requests a real client must service.
        m = msg["method"]
        result: Any
        if m == "workspace/configuration":
            result = [{} for _ in (msg["params"].get("items") or [{}])]  # defaults
        elif m in ("client/registerCapability", "client/unregisterCapability",
                   "window/workDoneProgress/create"):
            result = None
        else:
            result = None
        self._send({"jsonrpc": "2.0", "id": msg["id"], "result": result})

    def request(self, method: str, params: dict, timeout: float = 30.0) -> Any:
        with self._lock:
            self._id += 1
            rid = self._id
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        import time
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if rid in self._resp:
                    return self._resp.pop(rid).get("result")
            self._ev.wait(0.1); self._ev.clear()
        raise TimeoutError(method)

    def notify(self, method: str, params: dict) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    # --- lsp ---------------------------------------------------------------
    def initialize(self) -> None:
        self.request("initialize", {
            "processId": os.getpid(),
            "rootUri": self.root.as_uri(),
            "capabilities": {
                "textDocument": {
                    "documentSymbol": {"hierarchicalDocumentSymbolSupport": True},
                    "callHierarchy": {"dynamicRegistration": True},
                    "definition": {}, "references": {},
                },
                "workspace": {"configuration": True, "workspaceFolders": True},
            },
            "workspaceFolders": [{"uri": self.root.as_uri(), "name": self.root.name}],
        })
        self.notify("initialized", {})

    def did_open(self, path: Path) -> str:
        uri = path.as_uri()
        self.notify("textDocument/didOpen", {"textDocument": {
            "uri": uri, "languageId": "python", "version": 1,
            "text": path.read_text()}})
        return uri

    def shutdown(self) -> None:
        try:
            self.request("shutdown", {}, timeout=5)
            self.notify("exit", {})
        except Exception:
            pass
        self.proc.terminate()


def _find_symbol(syms: Any, name: str) -> dict | None:
    for s in syms or []:
        if s.get("name") == name:
            return s
        hit = _find_symbol(s.get("children") or [], name)
        if hit:
            return hit
    return None


def _fmt(items: Any, key: str) -> list[str]:
    out = []
    for it in items or []:
        node = it.get(key, {})
        frm = it.get("fromRanges") or it.get("to") or []
        out.append(f"{node.get('name','?')}  [{Path(node.get('uri','')).name}]")
    return out


def main(argv: list[str]) -> int:
    rel, name = argv[1], argv[2]
    root = Path.cwd()
    path = (root / rel).resolve()
    langserver = os.environ.get("PYRIGHT_LS", "pyright-langserver")
    c = Client([langserver, "--stdio"], root)
    try:
        c.initialize()
        uri = c.did_open(path)
        import time; time.sleep(1.5)   # let pyright index
        syms = c.request("textDocument/documentSymbol", {"textDocument": {"uri": uri}})
        sym = _find_symbol(syms, name)
        if not sym:
            print(f"symbol {name!r} not found in {rel}", file=sys.stderr)
            print("top-level symbols:", [s.get("name") for s in (syms or [])][:20])
            return 1
        pos = (sym.get("selectionRange") or sym.get("range"))["start"]
        print(f"symbol: {name}  at {rel}:{pos['line']+1}")
        prep = c.request("textDocument/prepareCallHierarchy",
                         {"textDocument": {"uri": uri}, "position": pos})
        if not prep:
            print("no call-hierarchy item (server may not support it)"); return 1
        item = prep[0]
        incoming = c.request("callHierarchy/incomingCalls", {"item": item})
        outgoing = c.request("callHierarchy/outgoingCalls", {"item": item})
        print(f"\n  called_by ({len(incoming or [])}):")
        for s in _fmt(incoming, "from"):
            print("   ←", s)
        print(f"\n  calls ({len(outgoing or [])}):")
        for s in _fmt(outgoing, "to"):
            print("   →", s)
    finally:
        c.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
