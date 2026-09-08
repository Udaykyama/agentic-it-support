"""Submit synthetic incidents to the authenticated API; never approve a generated runbook."""
import argparse
import os
import time
import uuid
from urllib.parse import urlsplit

import requests
from dotenv import load_dotenv

from app.config import secret

TICKETS = [
    {
        "title": "VPN connection timeout",
        "description": "The company VPN connection times out before connecting from home.",
        "submitter": "alice@example.com",
    },
    {
        "title": "VPN connection timeout on Ethernet",
        "description": "VPN times out before connecting, on both Ethernet and Wi-Fi.",
        "submitter": "bob@example.com",
    },
    {
        "title": "Repeated VPN connection timeout",
        "description": "VPN connection attempts time out repeatedly from a second home network.",
        "submitter": "carol@example.com",
    },
]


def main():
    load_dotenv(override=False)
    parser = argparse.ArgumentParser(description="Exercise NeuralDesk with an agent/admin access token and synthetic tickets.")
    parser.add_argument("--url", default=os.getenv("NEURALDESK_URL", "http://localhost:8000"))
    parser.add_argument("--wait-seconds", type=int, default=180)
    parser.add_argument("--generate-draft", action="store_true", help="Request a draft for the highest-ranked recurring issue")
    args = parser.parse_args()
    token = secret(os.environ, "NEURALDESK_TOKEN")
    if not token:
        parser.error("Set NEURALDESK_TOKEN or NEURALDESK_TOKEN_FILE to an OIDC agent/admin access token.")
    parsed = urlsplit(args.url)
    if (
        parsed.scheme not in {"http", "https"} or not parsed.hostname
        or parsed.username or parsed.password or parsed.query or parsed.fragment
        or parsed.path not in {"", "/"}
        or (parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"})
    ):
        parser.error("--url must be an HTTPS origin (HTTP is allowed only on loopback).")
    if args.wait_seconds < 1:
        parser.error("--wait-seconds must be positive.")
    base = args.url.rstrip("/") + "/api/v1"
    with requests.Session() as client:
        client.headers["Authorization"] = f"Bearer {token}"

        def call(method, path, **kwargs):
            for attempt in range(3):
                try:
                    response = client.request(method, base + path, timeout=10, **kwargs)
                except (requests.ConnectionError, requests.Timeout):
                    if attempt == 2:
                        raise
                    time.sleep(2)
                    continue
                if response.status_code == 429 and attempt < 2:
                    wait = max(1, min(60, int(response.headers.get("Retry-After", "60"))))
                    print(f"Shared rate limit reached; waiting {wait}s.")
                    time.sleep(wait)
                    continue
                response.raise_for_status()
                return response.json()
            raise RuntimeError("The request retry budget was exhausted.")

        try:
            identity = call("GET", "/me")
            print(f"Company: {identity['tenant']['name']}")
            if not set(identity["user"]["roles"]) & {"agent", "admin"}:
                parser.error("This synthetic, on-behalf-of demo requires an agent or admin role.")
            print("AI enabled:", identity["capabilities"]["ai_enabled"])
            pending = {}
            for payload in TICKETS:
                result = call(
                    "POST", "/tickets", json=payload,
                    headers={"Idempotency-Key": str(uuid.uuid4())},
                )
                ticket = result["ticket"]
                print(f"[{ticket['status']}] {ticket['title']} ({ticket['id']})")
                if ticket["status"] in {"pending", "processing"}:
                    pending[ticket["id"]] = ticket["title"]
            deadline = time.monotonic() + args.wait_seconds
            while pending and time.monotonic() < deadline:
                time.sleep(3)
                for identifier in list(pending):
                    ticket = call("GET", f"/tickets/{identifier}")["ticket"]
                    if ticket["status"] not in {"pending", "processing"}:
                        print(f"[{ticket['status']}] {pending.pop(identifier)}; reason={ticket['reason'] or ticket['error_code']}")
            if pending:
                print("Some tickets are still processing; inspect their IDs in the dashboard.")
                return 1
            insights = call("GET", "/insights")["recurring_issues"]
            print("\nRecurring incidents (heuristic priorities, not confirmed root causes):")
            for issue in insights:
                print(
                    f"  {issue['category']}/{issue['subcategory']}: {issue['ticket_count']} tickets, "
                    f"priority {issue['priority_score']}, knowledge gap={issue['knowledge_gap']}"
                )
            if args.generate_draft and insights:
                issue = insights[0]
                result = call("POST", "/runbooks/generate", json={
                    "category": issue["category"], "subcategory": issue["subcategory"],
                })
                print(f"Draft job: {result['job']['id']} ({result['job']['status']}); an admin must review before approval.")
            print("\nTicket outcomes:", call("GET", "/stats")["statuses"])
            print("Recommendations are not confirmed fixes. Escalation labels do not send notifications.")
        except requests.RequestException as error:
            response = error.response
            if response is not None:
                print(f"API request failed: HTTP {response.status_code}; request ID={response.headers.get('X-Request-ID', 'unavailable')}")
            else:
                print(f"API request failed ({type(error).__name__}). Check the URL and service availability.")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
