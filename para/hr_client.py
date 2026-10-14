"""Server-side HarnessRouter Cloud client (UHP mode). Standard library only.

Only the product server calls this; the browser never sees the key, a harness id or a session id.
Verified against the deployed service on 2026-10-11: POST /v1/responses streams `data:` lines
(no `event:` lines), the session id arrives in response.metadata.session_id on response.created,
and a finished session reports status "done".
"""
import http.client
import json
import socket
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field

from . import config


class HRError(Exception):
    """A classified failure. `code` is machine-readable; the message is safe to show a user."""

    def __init__(self, code, message, response_id=None, session_id=None):
        super().__init__(message)
        self.code, self.response_id, self.session_id = code, response_id, session_id


@dataclass
class RunResult:
    response_id: str
    session_id: str
    status: str
    text: str
    model: str = None
    usage: dict = field(default_factory=dict)
    elapsed: float = 0.0


def _status_error(status, body=b""):
    """Classify by the UHP error envelope's machine-readable code first, then by HTTP status."""
    try:
        err = json.loads(body or b"{}").get("error") or {}
    except (ValueError, AttributeError):
        err = {}
    etype, ecode = (err.get("type") if isinstance(err, dict) else None), (err.get("code") if isinstance(err, dict) else None)
    if etype == "billing_error" or status == 402:
        return HRError("billing", "HarnessRouter cannot bill this model on your plan (it needs a provider key). Choose another model under AI models.")
    if ecode == "one_at_a_time":
        return HRError("rate_limited", "HarnessRouter's plan limit on simultaneous tasks was reached.")
    if status == 401:
        return HRError("unauthorized", "The HarnessRouter key was rejected.")
    if status == 403:
        return HRError("forbidden", "The HarnessRouter key may not run this harness.")
    if status == 404:
        return HRError("not_found", "The configured harness was not found.")
    if status == 429:
        return HRError("rate_limited", "HarnessRouter is rate limiting requests.")
    if 500 <= status <= 599:
        return HRError("unavailable", "HarnessRouter is temporarily unavailable.")
    return HRError("bad_request", f"HarnessRouter refused the request (HTTP {status}).")


class Client:
    def __init__(self, base_url=None, api_key=None, uhp_version=None):
        self.base = (base_url or config.HR_BASE_URL).rstrip("/")
        self.key = api_key if api_key is not None else config.hr_api_key()
        self.version = uhp_version or config.HR_UHP_VERSION
        u = urllib.parse.urlparse(self.base)
        self._scheme, self._host, self._port, self._prefix = u.scheme, u.hostname, u.port, u.path.rstrip("/")

    def configured(self):
        return bool(self.key)

    def _conn(self, timeout):
        cls = http.client.HTTPSConnection if self._scheme == "https" else http.client.HTTPConnection
        return cls(self._host, self._port, timeout=timeout)

    def _headers(self, extra=None):
        return {"Authorization": "Bearer " + self.key, "Accept": "application/json",
                "UHP-Version": self.version, **(extra or {})}

    def _json(self, method, path, body=None, timeout=20):
        conn = self._conn(timeout)
        try:
            conn.request(method, self._prefix + path, body=json.dumps(body) if body is not None else None,
                         headers=self._headers({"Content-Type": "application/json"} if body is not None else None))
            resp = conn.getresponse()
            raw = resp.read(4_000_000)
            if resp.status >= 300:
                raise _status_error(resp.status, raw[:4000])
            ctype = resp.getheader("Content-Type") or ""
            if "ndjson" in ctype:
                return [json.loads(line) for line in raw.decode().splitlines() if line.strip()]
            return json.loads(raw or b"{}")
        except (OSError, http.client.HTTPException) as e:
            raise HRError("connection", "HarnessRouter could not be reached.") from e
        finally:
            conn.close()

    # ------------------------------------------------------------------ runs

    def run(self, harness_id, task_input, on_event=None, deadline_s=240, retries=2, model=None):
        """Run one bounded task to completion. Streams, and recovers by polling if the stream drops.

        `model` overrides the harness's default model for this run only (verified live on 2026-10-13).
        """
        if not self.key:
            raise HRError("not_configured", "HarnessRouter is not configured on this server.")
        emit = on_event or (lambda *_: None)
        started = time.monotonic()
        idem = str(uuid.uuid4())  # one key per task; reused only to retry this exact request
        payload = {"input": task_input, "stream": True, "metadata": {"harness_id": harness_id}}
        if model:
            payload["model"] = model
        body = json.dumps(payload)
        attempt = 0
        while True:
            try:
                return self._stream(body, idem, emit, started, deadline_s)
            except HRError as e:
                # A rate limit is retried until the deadline; other transient errors only `retries` times.
                # Nothing is retried once the task has started (response_id set): that is recovered, not resent.
                limited = e.code == "rate_limited"
                retryable = e.code in ("rate_limited", "unavailable", "connection") and e.response_id is None
                wait = 5 if limited else min(2 ** attempt, 4)
                if not retryable or (not limited and attempt >= retries) or time.monotonic() - started + wait > deadline_s - 5:
                    raise
                attempt += 1
                emit("retry", {"attempt": attempt, "code": e.code})
                time.sleep(wait)

    def _stream(self, body, idem, emit, started, deadline_s):
        conn = self._conn(timeout=min(60, deadline_s))
        ids = {"response_id": None, "session_id": None}
        text_parts, final = [], None
        try:
            try:
                conn.request("POST", self._prefix + "/v1/responses", body=body, headers=self._headers({
                    "Content-Type": "application/json", "Accept": "text/event-stream", "Idempotency-Key": idem}))
                resp = conn.getresponse()
            except (OSError, http.client.HTTPException) as e:
                raise HRError("connection", "HarnessRouter could not be reached.") from e
            if resp.status >= 300:
                raise _status_error(resp.status, resp.read(4000))
            emit("waiting", {})
            try:
                while True:
                    if time.monotonic() - started > deadline_s:
                        raise HRError("timeout", "The agent did not finish in time.", **ids)
                    line = resp.readline()
                    if not line:
                        break
                    if not line.startswith(b"data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == b"[DONE]":
                        break
                    try:
                        ev = json.loads(payload)
                    except ValueError:
                        continue
                    kind = ev.get("type", "")
                    r = ev.get("response") or {}
                    if kind == "response.created":
                        ids["response_id"] = r.get("id")
                        ids["session_id"] = (r.get("metadata") or {}).get("session_id")
                        emit("started", dict(ids))
                    elif kind == "response.output_item.added":
                        item = ev.get("item") or {}
                        emit("activity", {"item": item.get("type"), "name": item.get("name")})
                    elif kind == "response.output_text.delta":
                        text_parts.append(ev.get("delta", ""))
                    elif kind in ("response.completed", "response.incomplete", "response.failed"):
                        final = r
                        break
            except (socket.timeout, OSError, http.client.HTTPException):
                final = None  # stream dropped; recover below rather than resubmitting the task
        finally:
            conn.close()
        if final is None:
            if not ids["response_id"]:
                raise HRError("connection", "The connection to HarnessRouter dropped before the task started.")
            emit("recovering", dict(ids))
            final = self._poll_response(ids["response_id"], started, deadline_s, ids)
        return self._finish(final, "".join(text_parts), ids, started)

    def _poll_response(self, response_id, started, deadline_s, ids):
        while time.monotonic() - started < deadline_s:
            try:
                r = self._json("GET", f"/v1/responses/{response_id}")
            except HRError as e:
                if e.code not in ("connection", "unavailable", "rate_limited"):
                    raise
                r = {}
            if r.get("status") in ("completed", "incomplete", "failed", "cancelled"):
                return r
            time.sleep(2)
        raise HRError("timeout", "The agent did not finish in time.", **ids)

    def _finish(self, r, streamed_text, ids, started):
        status = r.get("status")
        sid = ids["session_id"] or (r.get("metadata") or {}).get("session_id")
        rid = r.get("id") or ids["response_id"]
        if status != "completed":
            code = "incomplete" if status == "incomplete" else "failed"
            raise HRError(code, f"The agent run ended as {status or 'unknown'}.", rid, sid)
        text = "".join(c.get("text", "") for item in r.get("output") or [] if item.get("type") == "message"
                       for c in item.get("content") or [] if c.get("type") == "output_text") or streamed_text
        return RunResult(rid, sid, status, text, r.get("served_model") or r.get("model"), r.get("usage") or {},
                         round(time.monotonic() - started, 1))

    # ------------------------------------------------------------------ sessions

    def cancel(self, response_id):
        return self._json("POST", f"/v1/responses/{response_id}/cancel", {})

    def turns(self, session_id):
        return self._json("GET", f"/v1/sessions/{urllib.parse.quote(session_id)}/turns").get("turns", [])

    def changed_files(self, session_id):
        return self._json("GET", f"/v1/sessions/{urllib.parse.quote(session_id)}/files?changed=true").get("files", [])

    def read_file(self, session_id, path, limit=200_000):
        conn = self._conn(20)
        try:
            conn.request("GET", self._prefix + f"/v1/sessions/{urllib.parse.quote(session_id)}/files/{urllib.parse.quote(path)}",
                         headers=self._headers({"Accept": "*/*"}))
            resp = conn.getresponse()
            raw = resp.read(limit + 1)
            if resp.status >= 300:
                raise _status_error(resp.status, raw[:4000])
            return raw[:limit].decode("utf-8", "replace")
        except (OSError, http.client.HTTPException) as e:
            raise HRError("connection", "HarnessRouter could not be reached.") from e
        finally:
            conn.close()
