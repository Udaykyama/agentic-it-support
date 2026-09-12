from __future__ import annotations

import argparse
import http.cookiejar
import json
import time
import uuid
from html.parser import HTMLParser
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit
from urllib.request import (
    HTTPCookieProcessor,
    HTTPRedirectHandler,
    ProxyHandler,
    Request,
    build_opener,
)

APP_ORIGIN = "http://localhost:8000"
ISSUER = "http://127.0.0.1:8081/realms/runbooksignal-demo"
PASSWORD = "RunbookSignal-Demo!"
NAMESPACE = uuid.UUID("f50cb8e0-cbe6-4bcf-8c5d-b216a94679a6")


def check(condition, message):
    if not condition:
        raise RuntimeError(message)


def stable_id(label):
    return str(uuid.uuid5(NAMESPACE, label))


class RedirectRecorder(HTTPRedirectHandler):
    def __init__(self):
        super().__init__()
        self.urls = []

    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        self.urls.append(new_url)
        return super().redirect_request(request, file_pointer, code, message, headers, new_url)


class LoginFormParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.action = None
        self.fields = {}
        self.in_login_form = False

    def handle_starttag(self, tag, attributes):
        values = dict(attributes)
        if tag == "form":
            identifier = values.get("id", "")
            action = values.get("action", "")
            self.in_login_form = identifier == "kc-form-login" or "login-actions/authenticate" in action
            if self.in_login_form:
                self.action = action
        elif tag == "input" and self.in_login_form:
            name = values.get("name")
            if name:
                self.fields[name] = values.get("value", "")

    def handle_endtag(self, tag):
        if tag == "form" and self.in_login_form:
            self.in_login_form = False


class Browser:
    def __init__(self):
        self.cookies = http.cookiejar.CookieJar()
        self.redirects = RedirectRecorder()
        self.opener = build_opener(
            ProxyHandler({}),
            HTTPCookieProcessor(self.cookies),
            self.redirects,
        )
        self.csrf = None
        self.identity = None

    def login(self, username, tenant_id, roles, email):
        self.redirects.urls.clear()
        with self.opener.open(f"{APP_ORIGIN}/auth/login", timeout=20) as response:
            page_url = response.geturl()
            page = response.read(262144).decode("utf-8")
        auth_url = next(
            (url for url in self.redirects.urls if url.startswith(f"{ISSUER}/protocol/openid-connect/auth?")),
            None,
        )
        check(auth_url is not None, "Application login did not redirect to the fixed local Keycloak issuer.")
        parameters = parse_qs(urlsplit(auth_url).query)
        check(parameters.get("code_challenge_method") == ["S256"], "The browser flow did not require S256 PKCE.")
        check(len(parameters.get("code_challenge", [""])[0]) >= 43, "The PKCE challenge is missing.")
        check(parameters.get("state", [""])[0], "The authorization state is missing.")
        check(parameters.get("nonce", [""])[0], "The OIDC nonce is missing.")
        check(parameters.get("prompt") == ["login"], "Demo login must require deliberate identity selection.")
        check(page_url.startswith(ISSUER), "The login form was not served by the fixed local issuer.")

        parser = LoginFormParser()
        parser.feed(page)
        check(parser.action, "The Keycloak login form could not be located.")
        fields = {
            **parser.fields,
            "username": username,
            "password": PASSWORD,
            "credentialId": parser.fields.get("credentialId", ""),
        }
        self.redirects.urls.clear()
        request = Request(
            urljoin(page_url, parser.action),
            data=urlencode(fields).encode("utf-8"),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with self.opener.open(request, timeout=30) as response:
            response.read(262144)
            final_url = response.geturl()
        callback_url = next(
            (url for url in self.redirects.urls if url.startswith(f"{APP_ORIGIN}/auth/callback?")),
            None,
        )
        check(callback_url is not None, "Keycloak did not return an authorization code to the application callback.")
        callback = parse_qs(urlsplit(callback_url).query)
        check(callback.get("state") == parameters.get("state"), "The callback state did not match the login state.")
        check(callback.get("code", [""])[0], "The callback authorization code is missing.")
        check(final_url.rstrip("/") == APP_ORIGIN, "The verified callback did not establish an application session.")
        check(any(cookie.name == "neuraldesk" for cookie in self.cookies), "The Redis-backed application session cookie is missing.")

        status, payload = self.call("/api/v1/me")
        check(status == 200, "The application session could not read its identity.")
        check(payload["tenant"]["id"] == tenant_id, "The signed tenant claim did not select the expected company.")
        check(set(payload["user"]["roles"]) == set(roles), "The signed application roles are incorrect.")
        check(payload["user"]["email"] == email, "The signed email claim is incorrect.")
        check(isinstance(payload.get("csrf_token"), str) and payload["csrf_token"], "The browser session has no CSRF token.")
        self.csrf = payload["csrf_token"]
        self.identity = payload
        return payload

    def call(self, path, method="GET", body=None, expected=(200,), csrf=True):
        headers = {"Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if method != "GET" and csrf and self.csrf:
            headers["X-CSRF-Token"] = self.csrf
        request = Request(f"{APP_ORIGIN}{path}", data=data, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=20) as response:
                status = response.status
                raw = response.read(262144)
        except HTTPError as error:
            status = error.code
            raw = error.read(262144)
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError as error:
            raise RuntimeError(f"{method} {path} returned non-JSON content.") from error
        allowed = {expected} if isinstance(expected, int) else set(expected)
        check(status in allowed, f"{method} {path} returned HTTP {status}, expected {sorted(allowed)}.")
        return status, payload


def wait_for_job(browser, job_id, timeout=45):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _, payload = browser.call(f"/api/v1/jobs/{job_id}")
        job = payload["job"]
        if job["status"] in {"completed", "failed"}:
            return job
        time.sleep(0.25)
    raise RuntimeError(f"Job {job_id} did not finish within {timeout} seconds.")


def wait_for_ticket(browser, ticket_id, timeout=45):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _, payload = browser.call(f"/api/v1/tickets/{ticket_id}")
        ticket = payload["ticket"]
        if ticket["status"] not in {"pending", "processing"}:
            return ticket
        time.sleep(0.25)
    raise RuntimeError(f"Ticket {ticket_id} did not finish processing within {timeout} seconds.")


def submit(browser, title, description, submitter):
    _, payload = browser.call(
        "/api/v1/tickets",
        method="POST",
        body={"title": title, "description": description, "submitter": submitter},
        expected=202,
    )
    return payload


def find_vpn_insight(browser):
    _, payload = browser.call("/api/v1/insights?days=14&min_tickets=3&limit=20")
    return next(
        (
            issue for issue in payload["recurring_issues"]
            if issue["category"] == "network" and issue["subcategory"] == "vpn_timeout"
        ),
        None,
    )


def run():
    requester = Browser()
    requester.login(
        "northstar-requester", "northstar-demo", ["requester"],
        "requester@northstar.example.com",
    )
    _, requester_tickets = requester.call("/api/v1/tickets")
    check(requester_tickets["pagination"]["total"] == 1, "Requester visibility includes another user's seeded ticket.")
    requester.call(
        "/api/v1/tickets",
        method="POST",
        body={
            "title": "Synthetic CSRF check",
            "description": "This request intentionally omits the browser CSRF token.",
            "submitter": "requester@northstar.example.com",
        },
        expected=403,
        csrf=False,
    )
    requester_result = submit(
        requester,
        "VPN classification smoke check",
        "Synthetic requester report: the VPN connection times out before connecting.",
        "requester@northstar.example.com",
    )
    requester_ticket = wait_for_ticket(requester, requester_result["ticket"]["id"])
    check(requester_ticket["status"] == "escalated", "A pre-approval VPN ticket should require human triage.")
    _, requester_tickets = requester.call("/api/v1/tickets")
    check(requester_tickets["pagination"]["total"] == 2, "Requester could not read both of their own tickets.")

    viewer = Browser()
    viewer.login(
        "northstar-viewer", "northstar-demo", ["viewer"],
        "viewer@northstar.example.com",
    )
    insight = find_vpn_insight(viewer)
    check(insight and insight["knowledge_gap"], "Northstar's recurring VPN knowledge gap is missing.")
    viewer.call(
        "/api/v1/tickets",
        method="POST",
        body={
            "title": "Viewer write must fail",
            "description": "Synthetic authorization boundary check.",
            "submitter": "viewer@northstar.example.com",
        },
        expected=403,
    )

    combined = Browser()
    combined.login(
        "northstar-requester-viewer", "northstar-demo", ["requester", "viewer"],
        "combined@northstar.example.com",
    )
    _, company_tickets = combined.call("/api/v1/tickets?limit=100")
    check(company_tickets["pagination"]["total"] > requester_tickets["pagination"]["total"], "Viewer role did not expand read visibility.")
    boundary_id = stable_id("ticket:northstar-role-boundary-failure")
    _, boundary = combined.call(f"/api/v1/tickets/{boundary_id}")
    check(boundary["ticket"]["can_retry"] is False, "Compound requester/viewer roles granted another user's retry.")
    combined.call(f"/api/v1/tickets/{boundary_id}/retry", method="POST", expected=404)

    agent = Browser()
    agent.login(
        "northstar-agent", "northstar-demo", ["agent"],
        "agent@northstar.example.com",
    )
    insight = find_vpn_insight(agent)
    check(insight and insight["knowledge_gap"], "The agent cannot see the expected VPN knowledge gap.")
    _, generation = agent.call(
        "/api/v1/runbooks/generate",
        method="POST",
        body={"category": "network", "subcategory": "vpn_timeout"},
        expected=202,
    )
    generated = wait_for_job(agent, generation["job"]["id"])
    check(generated["status"] == "completed", "Deterministic draft generation failed.")
    _, runbooks = agent.call("/api/v1/runbooks?limit=100")
    draft = next(
        (
            runbook for runbook in runbooks["runbooks"]
            if runbook["category"] == "network" and runbook["subcategory"] == "vpn_timeout"
        ),
        None,
    )
    check(draft and draft["status"] == "draft", "Generated knowledge was not held for human review.")

    admin = Browser()
    admin.login(
        "northstar-admin", "northstar-demo", ["admin"],
        "admin@northstar.example.com",
    )
    _, approval = admin.call(
        f"/api/v1/runbooks/{draft['id']}/approve",
        method="POST",
        expected=202,
    )
    approved_job = wait_for_job(admin, approval["job"]["id"])
    check(approved_job["status"] == "completed", "Runbook indexing did not complete.")
    _, runbooks = admin.call("/api/v1/runbooks?limit=100")
    approved = next(runbook for runbook in runbooks["runbooks"] if runbook["id"] == draft["id"])
    check(approved["status"] == "approved", "The reviewed runbook did not become approved.")

    recommended_result = submit(
        admin,
        "VPN times out in the synthetic sales walkthrough",
        "Synthetic report: the VPN connection times out before connecting from a managed laptop.",
        "walkthrough@northstar.example.com",
    )
    recommended = wait_for_ticket(admin, recommended_result["ticket"]["id"])
    check(recommended["status"] == "recommended", "Approved knowledge did not produce a recommendation.")
    check(recommended["resolution"] is None, "A recommendation was incorrectly recorded as a resolution.")

    failure_result = submit(
        admin,
        "VPN retry demonstration",
        "Synthetic retry demonstration: the VPN times out and the local model must return one invalid response.",
        "retry-demo@northstar.example.com",
    )
    failed = wait_for_ticket(admin, failure_result["ticket"]["id"])
    check(failed["status"] == "failed", "The controlled model response did not create a saved terminal failure.")
    check(failed["error_code"] == "invalid_model_response", "The controlled failure exposed the wrong error code.")
    _, retry = admin.call(
        f"/api/v1/tickets/{failed['id']}/retry",
        method="POST",
        expected=202,
    )
    retried_job = wait_for_job(admin, retry["job"]["id"])
    check(retried_job["status"] == "completed", "The explicit retry job did not complete.")
    recovered = wait_for_ticket(admin, failed["id"])
    check(recovered["status"] == "recommended", "The explicit retry did not recover through approved knowledge.")

    resolution = "Synthetic operator followed the reviewed checks and verified access to a permitted test resource."
    _, resolved_payload = admin.call(
        f"/api/v1/tickets/{failed['id']}/resolve",
        method="POST",
        body={"resolution": resolution},
    )
    resolved = resolved_payload["ticket"]
    check(resolved["status"] == "resolved" and resolved["resolution"] == resolution, "Explicit resolution was not recorded.")
    check(resolved["recommendation"], "Confirmed resolution erased the historical recommendation.")

    harbor = Browser()
    harbor.login(
        "harbor-admin", "harbor-demo", ["admin"],
        "admin@harbor.example.com",
    )
    harbor.call(f"/api/v1/tickets/{resolved['id']}", expected=404)
    _, harbor_tickets = harbor.call("/api/v1/tickets?limit=100")
    check(
        all("VPN" not in ticket["title"] for ticket in harbor_tickets["tickets"]),
        "Harbor's company view contains Northstar ticket text.",
    )
    _, harbor_insights = harbor.call("/api/v1/insights?days=14&min_tickets=3&limit=20")
    check(
        any(
            issue["subcategory"] == "warehouse_scanner_offline" and not issue["knowledge_gap"]
            for issue in harbor_insights["recurring_issues"]
        ),
        "Harbor's isolated approved scanner pattern is missing.",
    )

    _, stats = admin.call("/api/v1/stats")
    check(stats["statuses"]["resolved"] >= 1, "Confirmed resolution did not update tenant statistics.")
    print("RunbookSignal demo smoke passed: real browser OIDC, tenant/RBAC, knowledge workflow, retry, and confirmed resolution.")


def main():
    parser = argparse.ArgumentParser(description="Smoke-test only the loopback RunbookSignal synthetic demo.")
    parser.parse_args()
    run()


if __name__ == "__main__":
    main()
