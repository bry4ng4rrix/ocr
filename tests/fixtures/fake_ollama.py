"""Faux serveur Ollama (HTTP local, fil d'arrière-plan) : simule /api/tags, /api/show et /api/chat.

Les tests unitaires n'ont ainsi besoin d'aucun modèle chargé, tout en passant par la vraie pile HTTP
(urllib, délais, codes d'erreur). Le comportement de /api/chat est réglé par `FakeOllama.chat`.
"""
from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VALID_ANALYSIS = {
    "page_type": "text", "orientation_issue": "none", "perspective_issue": "none", "crop_issue": "none",
    "shadow_level": "high", "lighting_issue": "uneven", "blur_level": "none", "noise_level": "none",
    "contrast_level": "normal", "color_content": "grayscale", "reflection_issue": "none",
    "curvature_issue": "none", "recommended_actions": ["SHADOW_CORRECTION"], "manual_review_required": False,
    "reason": "Ombre marquée sur la moitié droite.",
}


@dataclass
class ChatReply:
    status: int = 200
    content: str | None = None          # message.content ; None = JSON de VALID_ANALYSIS
    delay_s: float = 0.0
    raw_body: bytes | None = None       # corps brut (réponse mal formée)


@dataclass
class FakeOllama:
    models: list[str] = field(default_factory=lambda: ["gemma3:4b"])
    capabilities: list[str] = field(default_factory=lambda: ["completion", "vision"])
    chat: Callable[[dict], ChatReply] = lambda payload: ChatReply()
    requests: list[tuple[str, dict]] = field(default_factory=list)
    url: str = ""


def start(fake: FakeOllama) -> tuple[ThreadingHTTPServer, threading.Thread]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:   # silence
            pass

        def _send(self, status: int, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self) -> None:
            fake.requests.append((self.path, {}))
            if self.path == "/api/tags":
                body = {"models": [{"name": name, "digest": f"digest-{name}"} for name in fake.models]}
                self._send(200, json.dumps(body).encode())
            else:
                self._send(404, b'{"error":"not found"}')

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}")
            fake.requests.append((self.path, payload))
            if self.path == "/api/show":
                if payload.get("model") not in fake.models:
                    self._send(404, json.dumps({"error": f"model '{payload.get('model')}' not found"}).encode())
                    return
                body = {"capabilities": fake.capabilities,
                        "details": {"parameter_size": "3.9B", "quantization_level": "Q4_K_M"}}
                self._send(200, json.dumps(body).encode())
            elif self.path == "/api/chat":
                if payload.get("model") not in fake.models:
                    self._send(404, json.dumps({"error": "model not found"}).encode())
                    return
                reply = fake.chat(payload)
                if reply.delay_s:
                    time.sleep(reply.delay_s)
                if reply.raw_body is not None:
                    self._send(reply.status, reply.raw_body)
                    return
                content = reply.content if reply.content is not None else json.dumps(VALID_ANALYSIS)
                body = {"model": payload["model"], "message": {"role": "assistant", "content": content},
                        "done": True, "eval_count": 10, "prompt_eval_count": 270}
                self._send(reply.status, json.dumps(body).encode() if reply.status == 200
                           else b'{"error":"server error"}')
            else:
                self._send(404, b'{"error":"not found"}')

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    fake.url = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def free_port_url() -> str:
    """URL d'un port local sur lequel rien n'écoute (serveur indisponible)."""
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}"
