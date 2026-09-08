from app.ingest import GenerateInput, ResolutionInput, TicketInput


def ref(name):
    return {"$ref": f"#/components/schemas/{name}"}


def obj(properties, required=None):
    return {"type": "object", "properties": properties, "required": required or list(properties)}


def array(item):
    return {"type": "array", "items": item}


def nullable(schema):
    return {"anyOf": [schema, {"type": "null"}]}


def specification(cookie_name):
    string = {"type": "string"}
    integer = {"type": "integer", "minimum": 0}
    timestamp = {"type": "string", "format": "date-time"}
    identifier = {"type": "string", "format": "uuid"}
    ratio = {"type": "number", "minimum": 0, "maximum": 1}
    counts = {"type": "object", "additionalProperties": integer}
    schemas = {
        model.__name__: model.model_json_schema(ref_template="#/components/schemas/{model}")
        for model in (TicketInput, GenerateInput, ResolutionInput)
    }
    schemas.update({
        "Error": obj({"error": obj({
            "code": string, "message": string, "request_id": nullable(identifier),
            "details": {},
        }, ["code", "message", "request_id"])}),
        "Pagination": obj({
            "limit": integer, "offset": integer, "total": integer, "has_more": {"type": "boolean"},
        }),
        "Ticket": obj({
            "id": identifier, "title": string, "description": string, "submitter": {"type": "string", "format": "email"},
            "created_at": timestamp, "updated_at": timestamp,
            "status": {"type": "string", "enum": ["pending", "processing", "recommended", "escalated", "resolved", "failed"]},
            "category": nullable(string), "subcategory": nullable(string), "confidence": nullable(ratio),
            "recommendation": nullable(string), "resolution": nullable(string), "runbook_id": nullable(identifier),
            "assigned_to": nullable(string), "reason": nullable(string), "error_code": nullable(string),
            "resolved_at": nullable(timestamp),
            "can_retry": {"type": "boolean", "description": "Whether the current identity owns a failed ticket or has an operator role. AI must also be enabled."},
        }),
        "Runbook": obj({
            "id": identifier, "category": string, "subcategory": string, "title": string,
            "problem": string, "root_cause": string, "steps": string, "prevention": string,
            "status": {"type": "string", "enum": ["draft", "approving", "approved", "rejected"]},
            "source_ticket_ids": array(identifier), "created_at": timestamp,
            "approved_by": nullable(string), "approved_at": nullable(timestamp), "error_code": nullable(string),
        }),
        "Job": obj({
            "id": identifier, "kind": {"type": "string", "enum": ["classify_ticket", "generate_runbook", "approve_runbook"]},
            "resource_id": nullable(identifier),
            "status": {"type": "string", "enum": ["queued", "running", "retrying", "completed", "failed"]},
            "attempts": integer, "error_code": nullable(string), "created_at": timestamp, "updated_at": timestamp,
        }),
        "Stats": obj({
            "total": integer, "statuses": counts, "categories": counts, "runbooks": counts, "resolution_rate": ratio,
        }),
        "Incident": obj({
            "category": string, "subcategory": string, "ticket_count": integer,
            "previous_count": integer, "unresolved_count": integer, "escalation_count": integer,
            "escalation_rate": ratio, "change_percent": nullable({"type": "number"}), "priority_score": integer,
            "knowledge_gap": {"type": "boolean"}, "approved_runbook_count": integer, "draft_runbook_count": integer,
            "recommended_action": string, "source_ticket_ids": array(identifier),
        }),
        "Insights": obj({
            "window": obj({
                "days": integer, "start": timestamp, "end": timestamp,
                "previous_start": timestamp, "min_tickets": integer,
            }),
            "recurring_issues": array(ref("Incident")),
        }),
        "AuditEvent": obj({
            "id": identifier, "actor": string, "action": string, "resource_id": nullable(identifier),
            "request_id": identifier, "details": {"type": "object"}, "created_at": timestamp,
        }),
        "Me": obj({
            "user": obj({
                "subject": string, "tenant_id": string, "roles": array({
                    "type": "string", "enum": ["requester", "viewer", "agent", "admin"],
                }), "expires_at": integer, "email": nullable(string),
            }),
            "tenant": obj({"id": string, "name": string}), "csrf_token": nullable(string),
            "capabilities": obj({"ai_enabled": {"type": "boolean"}}),
        }),
    })
    common_errors = {
        str(code): {"description": description, "content": {"application/json": {"schema": ref("Error")}}}
        for code, description in (
            (400, "Invalid JSON, input fields, or query parameters"),
            (401, "Missing, invalid, or expired identity"),
            (403, "Insufficient role, invalid CSRF token, or disabled/unprovisioned tenant"),
            (404, "Resource not found within the caller's visibility"),
            (409, "Conflicting idempotency key, insufficient source tickets, or invalid state"),
            (413, "Request body exceeds 32768 bytes"),
            (415, "JSON content type required"),
            (429, "Shared request or AI-work limit exceeded"),
            (500, "Unexpected failure; report the request ID"),
            (503, "Required dependency unavailable or AI explicitly disabled"),
        )
    }
    common_errors["429"]["headers"] = {
        "Retry-After": {"description": "Seconds until the shared limit resets", "schema": integer},
    }
    paging = [
        {"name": "limit", "in": "query", "schema": {"type": "integer", "default": 25, "minimum": 1, "maximum": 100}},
        {"name": "offset", "in": "query", "schema": {"type": "integer", "default": 0, "minimum": 0, "maximum": 10000}},
    ]
    csrf = {
        "name": "X-CSRF-Token", "in": "header", "required": False, "schema": string,
        "description": "Required for cookie-authenticated writes. Obtain from /api/v1/me. Not required for bearer clients.",
    }

    def operation(summary, roles, response, *, body=None, statuses=(200,), parameters=None, write=False, description=""):
        responses = dict(common_errors)
        for status in statuses:
            responses[str(status)] = {
                "description": "Accepted for asynchronous processing" if status == 202 else "Successful response",
                "content": {"application/json": {"schema": response}},
            }
        result = {
            "summary": summary, "description": f"Roles: {roles}. {description}".strip(),
            "security": [{"bearerAuth": []}, {"sessionAuth": []}],
            "responses": responses, "parameters": list(parameters or []),
        }
        if write:
            result["parameters"].append(csrf)
        if body:
            result["requestBody"] = {"required": True, "content": {"application/json": {"schema": ref(body)}}}
        return result

    def item_parameter(name):
        return {"name": name, "in": "path", "required": True, "schema": identifier}

    ticket_response = obj({"ticket": ref("Ticket")})
    job_response = obj({"job": ref("Job")})
    paths = {
        "/api/v1/me": {"get": operation("Current identity, company, and CSRF token", "all", ref("Me"))},
        "/api/v1/tickets": {
            "get": operation(
                "List visible tickets", "all", obj({"tickets": array(ref("Ticket")), "pagination": ref("Pagination")}),
                parameters=paging + [{
                    "name": "status", "in": "query", "schema": schemas["Ticket"]["properties"]["status"],
                }],
                description="Requesters see only tickets created by their subject. Staff see their company. Stable descending creation-time/ID ordering.",
            ),
            "post": operation(
                "Accept a ticket and durably enqueue classification", "requester, agent, admin",
                obj({"ticket": ref("Ticket"), "job": nullable(ref("Job")), "duplicate": {"type": "boolean"}}),
                body="TicketInput", statuses=(200, 201, 202), write=True,
                parameters=[{
                    "name": "Idempotency-Key", "in": "header", "schema": {"type": "string", "maxLength": 128, "pattern": "^[A-Za-z0-9_.:-]+$"},
                    "description": "Optional retry key scoped to company and subject. Reuse only for an identical normalized request. Keys remain valid while the ticket is retained.",
                }],
                description="202 queues AI work; poll the ticket or job. AI_ENABLED=false explicitly routes for manual triage (201), without model calls. A replay returns 200; changed payload with the same key returns 409. Requesters must use their IdP email.",
            ),
        },
        "/api/v1/tickets/{ticket_id}": {
            "get": operation("Get a visible ticket and its processing outcome", "all", ticket_response,
                             parameters=[item_parameter("ticket_id")]),
        },
        "/api/v1/tickets/{ticket_id}/resolve": {
            "post": operation(
                "Record an operator-confirmed resolution", "agent, admin", ticket_response,
                body="ResolutionInput", write=True, parameters=[item_parameter("ticket_id")],
                description="A recommendation alone never counts as resolved. A completed in-flight AI job cannot overwrite this confirmation.",
            ),
        },
        "/api/v1/tickets/{ticket_id}/retry": {
            "post": operation(
                "Retry a failed visible ticket", "requester, agent, admin",
                obj({"ticket": ref("Ticket"), "job": ref("Job")}), statuses=(202,), write=True,
                parameters=[item_parameter("ticket_id")],
                description="Requesters may retry only their own tickets, even when they also have company-wide viewer access.",
            ),
        },
        "/api/v1/runbooks": {
            "get": operation(
                "List runbooks for the company", "all",
                obj({"runbooks": array(ref("Runbook")), "pagination": ref("Pagination")}), parameters=paging,
                description="Requesters see only approved content, without source ticket IDs or reviewer identity. Staff may inspect drafts and rejected records.",
            ),
        },
        "/api/v1/runbooks/generate": {
            "post": operation(
                "Draft a runbook for a recurring issue", "agent, admin", job_response, body="GenerateInput",
                statuses=(200, 202), write=True,
                description="Requires at least 3 matching tickets in the last 30 days. At most 10 source tickets are used. The same source set reuses its durable job (200). Failed jobs must be retried explicitly. Drafts cannot be recommended.",
            ),
        },
        "/api/v1/runbooks/{runbook_id}/approve": {
            "post": operation(
                "Approve a reviewed draft and enqueue its embedding", "admin",
                obj({"runbook": ref("Runbook"), "job": ref("Job")}), statuses=(202,), write=True,
                parameters=[item_parameter("runbook_id")],
                description="Only a draft may be approved. It remains approving until indexing succeeds; a failed job returns it to draft. Approval authorizes future recommendations, not execution.",
            ),
        },
        "/api/v1/runbooks/{runbook_id}/revoke": {
            "post": operation(
                "Reject or revoke a runbook", "admin", obj({"runbook": ref("Runbook")}),
                write=True, parameters=[item_parameter("runbook_id")],
                description="Stops future recommendations, including an in-flight approval. Does not erase historical ticket recommendations.",
            ),
        },
        "/api/v1/jobs/{job_id}": {
            "get": operation("Inspect a visible background job", "all", job_response, parameters=[item_parameter("job_id")]),
        },
        "/api/v1/jobs/{job_id}/retry": {
            "post": operation(
                "Retry a failed draft-generation or approval job", "agent, admin (approval requires admin)",
                job_response, statuses=(202,), write=True, parameters=[item_parameter("job_id")],
                description="Use ticket retry for classification. Revoked runbooks cannot be reapproved by replaying an old job.",
            ),
        },
        "/api/v1/stats": {
            "get": operation("Visible ticket outcomes and knowledge-base counts", "all", ref("Stats")),
        },
        "/api/v1/insights": {
            "get": operation(
                "Rank recurring incidents and identify knowledge gaps", "viewer, agent, admin", ref("Insights"),
                parameters=[
                    {"name": "days", "in": "query", "schema": {"type": "integer", "minimum": 1, "maximum": 90, "default": 14}},
                    {"name": "min_tickets", "in": "query", "schema": {"type": "integer", "minimum": 3, "maximum": 1000, "default": 3}},
                    {"name": "limit", "in": "query", "schema": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20}},
                ],
                description="Groups by category/subcategory and compares equal adjacent UTC windows. Priority = unresolved*3 + max(current-previous,0)*2 + current. Failed tickets count toward escalation rate. A gap means no approved runbook with the same issue label, not proof that semantic retrieval would fail. Labels and scores are triage heuristics, not causal diagnoses.",
            ),
        },
        "/api/v1/audit": {
            "get": operation(
                "Read company audit events", "admin",
                obj({"events": array(ref("AuditEvent")), "pagination": ref("Pagination")}), parameters=paging,
            ),
        },
        "/auth/logout": {
            "post": {
                **operation("End the local browser session", "signed-in browser user", obj({"status": string}), write=True),
                "security": [{"sessionAuth": []}],
            },
        },
    }
    for path in ("/health/live", "/health/ready"):
        paths[path] = {"get": {
            "summary": "Process liveness" if path.endswith("live") else "Database and Redis readiness",
            "security": [], "responses": {
                "200": {"description": "Healthy", "content": {"application/json": {"schema": obj({"status": string})}}},
                "503": common_errors["503"],
            },
        }}
    paths["/metrics"] = {"get": {
        "summary": "Prometheus metrics (one scrape target per API replica)",
        "security": [{"metricsAuth": []}],
        "responses": {"200": {"description": "Prometheus exposition", "content": {"text/plain": {"schema": string}}}, "401": common_errors["401"]},
    }}
    return {
        "openapi": "3.1.0", "info": {
            "title": "NeuralDesk IT Intelligence API", "version": "1.0.0",
            "description": (
                "Tenant identity and roles come only from signed claims issued by the configured OIDC provider. "
                "Companies must be provisioned by an operator. API JWTs require RS256, issuer, audience, subject, issued-at, and expiration. "
                "Browser sign-in starts at /auth/login, returns via /auth/callback, and uses Redis sessions with CSRF-protected writes. "
                "All timestamps are UTC. Errors and responses include X-Request-ID. Request bodies are limited to 32768 bytes. "
                "Unversioned legacy endpoints return 410 and never expose data or trigger processing."
            ),
        },
        "servers": [{"url": "/"}],
        "paths": paths,
        "components": {
            "schemas": schemas,
            "securitySchemes": {
                "bearerAuth": {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"},
                "sessionAuth": {"type": "apiKey", "in": "cookie", "name": cookie_name},
                "metricsAuth": {"type": "http", "scheme": "bearer", "description": "Separate METRICS_TOKEN, not an OIDC token."},
            },
        },
    }
