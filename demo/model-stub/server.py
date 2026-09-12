from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_BODY_BYTES = 65536
VECTOR_SIZE = 1536
HOST = "0.0.0.0"
PORT = 8080


class DemoState:
    def __init__(self):
        self.lock = threading.Lock()
        self.failed_once = set()

    def should_fail_once(self, key):
        with self.lock:
            if key in self.failed_once:
                return False
            self.failed_once.add(key)
            return True

    def reset(self):
        with self.lock:
            self.failed_once.clear()


STATE = DemoState()


def classification(text):
    normalized = text.lower()
    if "vpn" in normalized:
        return {"category": "network", "subcategory": "vpn_timeout", "confidence": 0.96}
    if "warehouse scanner" in normalized or "handheld scanner" in normalized:
        return {"category": "hardware", "subcategory": "warehouse_scanner_offline", "confidence": 0.95}
    if "account locked" in normalized or "locked account" in normalized:
        return {"category": "access", "subcategory": "account_locked", "confidence": 0.94}
    if "application crash" in normalized or "desktop client crash" in normalized:
        return {"category": "software", "subcategory": "application_crash", "confidence": 0.93}
    return {"category": "other", "subcategory": "needs_human_review", "confidence": 0.41}


def draft(category, subcategory):
    if (category, subcategory) == ("network", "vpn_timeout"):
        return {
            "title": "Review recurring VPN timeout reports",
            "problem": "Synthetic users report that the VPN connection times out before a session is established.",
            "root_cause": "Hypothesis: the timeout may involve service availability, network reachability, or an expired client session. Confirm the evidence before changing access or device settings.",
            "steps": (
                "1. Verify the requester identity and confirm the affected device is company managed.\n"
                "2. Check the approved service-status source and record whether a broader incident exists.\n"
                "3. Reproduce the timeout without requesting passwords, recovery codes, or private keys.\n"
                "4. Refresh only the approved VPN client session and retry once.\n"
                "5. Confirm access to an allowed internal test resource; do not treat connection alone as resolution.\n"
                "6. Escalate to the network owner when the timeout remains or the evidence is inconclusive."
            ),
            "prevention": "Review confirmed outcomes for this pattern and update the runbook only after a human owner validates recurring evidence.",
        }
    return {
        "title": f"Review recurring {subcategory.replace('_', ' ')} reports",
        "problem": f"Synthetic {category} tickets share the pattern {subcategory.replace('_', ' ')}.",
        "root_cause": "Hypothesis only: the available synthetic reports do not establish a single cause. Validate the environment and confirmed outcomes first.",
        "steps": (
            "1. Verify the requester and the affected asset or account.\n"
            "2. Review the source tickets and approved system status.\n"
            "3. Reproduce the reported behavior without weakening security controls.\n"
            "4. Record the observed result and escalate when the evidence is inconclusive."
        ),
        "prevention": "Have the operational owner review confirmed outcomes before changing this guidance.",
    }


def vector_for(text):
    normalized = text.lower()
    if "vpn" in normalized:
        index = 0
    elif "warehouse scanner" in normalized or "handheld scanner" in normalized:
        index = 1
    elif "account locked" in normalized or "locked account" in normalized:
        index = 2
    elif "application crash" in normalized or "desktop client crash" in normalized:
        index = 3
    else:
        digest = hashlib.sha256(normalized.encode("utf-8")).digest()
        index = 4 + int.from_bytes(digest[:2], "big") % (VECTOR_SIZE - 4)
    vector = [0.0] * VECTOR_SIZE
    vector[index] = 1.0
    return vector


def completion_response(payload):
    messages = payload.get("messages")
    if not isinstance(messages, list):
        raise ValueError("messages must be an array")
    system = next(
        (item.get("content") for item in messages if isinstance(item, dict) and item.get("role") == "system"),
        None,
    )
    user = next(
        (item.get("content") for item in reversed(messages) if isinstance(item, dict) and item.get("role") == "user"),
        None,
    )
    if not isinstance(system, str) or not isinstance(user, str):
        raise ValueError("system and user messages are required")
    try:
        request_data = json.loads(user)
    except json.JSONDecodeError as error:
        raise ValueError("user content must contain JSON") from error
    if not isinstance(request_data, dict):
        raise ValueError("user content must contain an object")

    if system.startswith("Classify an IT support ticket."):
        text = "\n".join(
            str(request_data.get(field, "")) for field in ("title", "description", "category_hint")
        )
        failure_key = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if "retry demonstration" in text.lower() and STATE.should_fail_once(failure_key):
            content = "intentional synthetic invalid response for the retry demonstration"
        else:
            content = json.dumps(classification(text), separators=(",", ":"))
    elif system.startswith("Draft a runbook for human IT review"):
        category = request_data.get("category")
        subcategory = request_data.get("subcategory")
        if not isinstance(category, str) or not isinstance(subcategory, str):
            raise ValueError("draft category and subcategory are required")
        content = json.dumps(draft(category, subcategory), separators=(",", ":"))
    else:
        raise ValueError("unsupported system instruction")

    model = payload.get("model")
    if not isinstance(model, str) or not model:
        raise ValueError("model is required")
    return {
        "id": "chatcmpl-runbooksignal-demo",
        "object": "chat.completion",
        "created": 0,
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def embedding_response(payload):
    value = payload.get("input")
    values = value if isinstance(value, list) else [value]
    if not values or not all(isinstance(item, str) and item for item in values):
        raise ValueError("input must contain nonempty text")
    if payload.get("dimensions") not in (None, VECTOR_SIZE):
        raise ValueError("only 1536-dimensional embeddings are supported")
    model = payload.get("model")
    if not isinstance(model, str) or not model:
        raise ValueError("model is required")
    return {
        "object": "list",
        "model": model,
        "data": [
            {"object": "embedding", "index": index, "embedding": vector_for(item)}
            for index, item in enumerate(values)
        ],
        "usage": {"prompt_tokens": 0, "total_tokens": 0},
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "RunbookSignalDemoStub"
    sys_version = ""

    def log_message(self, _format, *_args):
        return

    def send_json(self, status, payload):
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self.send_json(200, {"status": "ready", "provider": "synthetic-demo-stub"})
        else:
            self.send_json(404, {"error": {"message": "Not found", "type": "invalid_request_error"}})

    def read_json(self):
        raw_length = self.headers.get("Content-Length")
        if raw_length is None or not raw_length.isdigit():
            raise ValueError("a valid Content-Length is required")
        length = int(raw_length)
        if not 1 <= length <= MAX_BODY_BYTES:
            raise ValueError("request body size is invalid")
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise ValueError("Content-Type must be application/json")
        try:
            payload = json.loads(self.rfile.read(length))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("request body must be valid JSON") from error
        if not isinstance(payload, dict):
            raise ValueError("request body must be an object")
        return payload

    def authorized(self):
        expected = f"Bearer {os.environ['MODEL_API_KEY']}"
        return hmac.compare_digest(self.headers.get("Authorization", ""), expected)

    def do_POST(self):
        if self.path == "/__demo/reset":
            expected = os.environ["RESET_TOKEN"]
            if not hmac.compare_digest(self.headers.get("X-Demo-Control-Token", ""), expected):
                self.send_json(403, {"error": {"message": "Forbidden", "type": "authentication_error"}})
                return
            STATE.reset()
            self.send_json(200, {"status": "reset"})
            return
        if self.path not in {"/v1/chat/completions", "/v1/embeddings"}:
            self.send_json(404, {"error": {"message": "Not found", "type": "invalid_request_error"}})
            return
        if not self.authorized():
            self.send_json(401, {"error": {"message": "Unauthorized", "type": "authentication_error"}})
            return
        try:
            payload = self.read_json()
            response = (
                completion_response(payload)
                if self.path == "/v1/chat/completions"
                else embedding_response(payload)
            )
        except ValueError as error:
            self.send_json(400, {"error": {"message": str(error), "type": "invalid_request_error"}})
            return
        self.send_json(200, response)


def main():
    for name in ("MODEL_API_KEY", "RESET_TOKEN"):
        if not os.environ.get(name):
            raise RuntimeError(f"{name} is required")
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
