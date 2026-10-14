"""A fake HarnessRouter that speaks the wire format recorded from the live service on 2026-10-11.

Each harness id maps to a scripted reply (a string, or a callable taking the task input). Modes:
  "drop":   send response.created, then close the stream (client must recover by polling)
  "fail":   end the stream with response.failed
  "http500" / "http401": refuse the POST
"""
import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeHR:
    def __init__(self):
        self.replies, self.modes, self.calls, self.responses, self.files = {}, {}, [], {}, {}
        handler = self._handler()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def _handler(self):
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, code, body):
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                parts = self.path.split("?")[0].strip("/").split("/")
                if parts[:2] == ["v1", "responses"] and parts[2] in fake.responses:
                    return self._json(200, fake.responses[parts[2]])
                if parts[:2] == ["v1", "sessions"] and parts[-1] == "turns":
                    r = next((r for r in fake.responses.values() if r["metadata"]["session_id"] == parts[2]), None)
                    text = r["output"][0]["content"][0]["text"] if r and r["output"] else ""
                    return self._json(200, {"turns": [{"id": r and r["id"], "status": "completed", "assistant": text,
                                                       "tools": [], "files": [], "elapsed": 1.0, "served_model": "fake"}]})
                if parts[:2] == ["v1", "sessions"] and parts[-1] == "files":
                    files = fake.files.get(parts[2], {})
                    return self._json(200, {"files": [{"path": p} for p in files]})
                if parts[:2] == ["v1", "sessions"] and len(parts) >= 5 and parts[3] == "files":
                    body = fake.files.get(parts[2], {}).get("/".join(parts[4:]))
                    if body is None:
                        return self._json(404, {})
                    raw = body.encode()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    return self.wfile.write(raw)
                self._json(404, {"detail": "not found"})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                hid = body["metadata"]["harness_id"]
                fake.calls.append({"harness_id": hid, "input": body["input"], "model": body.get("model"), "headers": dict(self.headers)})
                mode = fake.modes.get(hid)
                if mode == "http500":
                    return self._json(500, {"detail": "boom"})
                if mode == "http401":
                    return self._json(401, {"detail": "nope"})
                if mode == "http402":
                    return self._json(402, {"error": {"type": "billing_error", "code": "no_key", "message": "m"}, "detail": "m"})
                reply = fake.replies.get(hid, "{}")
                text = reply(body["input"]) if callable(reply) else reply
                rid, sid = f"resp_{uuid.uuid4().hex}", f"hsess{uuid.uuid4().hex}"
                done = {"id": rid, "status": "failed" if mode == "fail" else "completed", "model": "fake",
                        "served_model": "fake-1", "metadata": {"session_id": sid}, "usage": {},
                        "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}]}
                fake.responses[rid] = done
                if isinstance(fake.replies.get(f"{hid}:files"), dict):
                    fake.files[sid] = fake.replies[f"{hid}:files"]
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.end_headers()

                def send(ev):
                    self.wfile.write(b"data: " + json.dumps(ev).encode() + b"\n\n")
                    self.wfile.flush()

                send({"type": "response.created", "response": {"id": rid, "status": "in_progress", "metadata": {"session_id": sid}, "output": []}})
                if mode == "drop":
                    return
                send({"type": "response.output_item.added", "item": {"type": "message"}})
                send({"type": "response.output_text.delta", "delta": text})
                send({"type": "response.failed" if mode == "fail" else "response.completed", "response": done})

        return H
