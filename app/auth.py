from __future__ import annotations

import re
import math
import secrets
import time
from dataclasses import dataclass
from functools import wraps
from typing import Optional
from urllib.parse import urlsplit

import jwt
from authlib.integrations.base_client.errors import OAuthError
from authlib.integrations.flask_client import OAuth
from authlib.jose.errors import JoseError
from flask import Blueprint, current_app, g, redirect, request, session
from jwt import PyJWKClient
from jwt.exceptions import InvalidTokenError, PyJWKClientConnectionError, PyJWKClientError
from requests.exceptions import RequestException

from app.db import audit
from app.errors import APIError
from app.rate_limit import enforce_limit

auth = Blueprint("auth", __name__)
TENANT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
ROLES = {"requester", "viewer", "agent", "admin"}
READ_COMPANY = {"viewer", "agent", "admin"}
WRITE_TICKETS = {"requester", "agent", "admin"}
OPERATE = {"agent", "admin"}


@dataclass(frozen=True)
class Principal:
    subject: str
    tenant_id: str
    roles: tuple
    expires_at: int
    email: Optional[str] = None

    def has_role(self, roles):
        return bool(set(self.roles) & set(roles))

    def to_dict(self):
        return {
            "subject": self.subject, "tenant_id": self.tenant_id,
            "roles": list(self.roles), "expires_at": self.expires_at, "email": self.email,
        }


def principal_from_claims(claims, settings):
    tenant_id = claims.get(settings.oidc_tenant_claim)
    subject = claims.get("sub")
    roles = claims.get(settings.oidc_roles_claim)
    expiry = claims.get("exp")
    if not isinstance(tenant_id, str) or not TENANT_ID_PATTERN.fullmatch(tenant_id):
        raise APIError(403, "invalid_tenant_claim", "The identity provider must supply a valid company claim.")
    if not isinstance(subject, str) or not subject or len(subject) > 255:
        raise APIError(401, "invalid_identity", "The token does not contain a valid subject.")
    if not isinstance(roles, list) or not roles or not all(isinstance(role, str) for role in roles):
        raise APIError(403, "missing_roles", "The identity provider must assign application roles.")
    allowed_roles = tuple(sorted(set(roles) & ROLES))
    if not allowed_roles:
        raise APIError(403, "missing_roles", "Your account has no application role.")
    if isinstance(expiry, bool) or not isinstance(expiry, (int, float)) or not math.isfinite(expiry) or expiry <= time.time():
        raise APIError(401, "session_expired", "Your sign-in has expired. Sign in again.")
    email = claims.get("email")
    if not isinstance(email, str) or len(email) > 254:
        email = None
    return Principal(subject, tenant_id, allowed_roles, int(expiry), email)


class IdentityProvider:
    def __init__(self, app, settings):
        self.settings = settings
        oauth = OAuth(app)
        self.client = oauth.register(
            "company",
            client_id=settings.oidc_client_id,
            client_secret=settings.oidc_client_secret,
            server_metadata_url=f"{settings.oidc_issuer.rstrip('/')}/.well-known/openid-configuration",
            client_kwargs={
                "scope": "openid profile email",
                "code_challenge_method": "S256",
                "default_timeout": 10,
            },
        )
        self.jwks = None

    def metadata(self):
        try:
            metadata = self.client.load_server_metadata()
        except (RequestException, ValueError) as error:
            raise APIError(503, "identity_unavailable", "The sign-in provider is temporarily unavailable.") from error
        if metadata.get("issuer") != self.settings.oidc_issuer:
            raise APIError(503, "identity_configuration_error", "The sign-in provider returned an unexpected issuer.")
        for field in ("jwks_uri", "authorization_endpoint", "token_endpoint"):
            value = metadata.get(field)
            if not isinstance(value, str):
                raise APIError(503, "identity_configuration_error", "The sign-in provider metadata is incomplete.")
            parsed = urlsplit(value)
            if (
                parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password
                or (self.settings.environment == "production" and parsed.scheme != "https")
            ):
                raise APIError(503, "identity_configuration_error", "The sign-in provider endpoints are invalid.")
        if self.jwks is None:
            self.jwks = PyJWKClient(metadata["jwks_uri"], timeout=10, lifespan=300)
        return metadata

    def verify(self, token, audience=None, expected_nonce=None):
        if not isinstance(token, str) or len(token) > 16384:
            raise APIError(401, "invalid_token", "Supply a valid bearer access token.")
        try:
            header = jwt.get_unverified_header(token)
            if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
                raise APIError(401, "invalid_token", "A signed RS256 token with a key ID is required.")
            if not 1 <= len(header["kid"]) <= 200:
                raise APIError(401, "invalid_token", "The token key ID is invalid.")
            self.metadata()
            key = self.jwks.get_signing_key_from_jwt(token).key
            claims = jwt.decode(
                token, key, algorithms=["RS256"],
                issuer=self.settings.oidc_issuer,
                audience=audience or self.settings.oidc_audience,
                leeway=30, options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except PyJWKClientConnectionError as error:
            raise APIError(503, "identity_unavailable", "The sign-in provider is temporarily unavailable.") from error
        except (InvalidTokenError, PyJWKClientError) as error:
            raise APIError(401, "invalid_token", "The access token is invalid or expired.") from error
        if expected_nonce is not None:
            nonce = claims.get("nonce")
            if not isinstance(nonce, str) or not secrets.compare_digest(nonce.encode("utf-8"), expected_nonce.encode("utf-8")):
                raise APIError(401, "login_failed", "The sign-in nonce could not be verified.")
        return principal_from_claims(claims, self.settings)


def require_roles(*roles):
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            principal = getattr(g, "principal", None)
            if principal is None:
                raise APIError(401, "authentication_required", "Sign in to continue.")
            if roles and not principal.has_role(roles):
                raise APIError(403, "forbidden", "Your role does not allow this action.")
            return function(*args, **kwargs)
        return wrapped
    return decorate


def verify_csrf():
    expected = session.get("csrf_token")
    supplied = request.headers.get("X-CSRF-Token", "")
    if not isinstance(expected, str) or not secrets.compare_digest(expected.encode("utf-8"), supplied.encode("utf-8")):
        raise APIError(403, "csrf_failed", "Refresh this page before submitting the request again.")


def authenticate_request():
    settings = current_app.config["SETTINGS"]
    header = request.headers.get("Authorization")
    if header is not None:
        parts = header.split()
        if len(parts) != 2 or parts[0].lower() != "bearer":
            raise APIError(401, "invalid_token", "Use the Authorization: Bearer header.")
        principal = current_app.extensions["identity"].verify(parts[1])
        g.authentication_method = "bearer"
    else:
        stored = session.get("principal")
        if not isinstance(stored, dict):
            raise APIError(401, "authentication_required", "Sign in to continue.")
        claims = {
            "sub": stored.get("subject"), "exp": stored.get("expires_at"), "email": stored.get("email"),
            settings.oidc_tenant_claim: stored.get("tenant_id"),
            settings.oidc_roles_claim: stored.get("roles"),
        }
        try:
            principal = principal_from_claims(claims, settings)
        except APIError:
            session.clear()
            raise
        g.authentication_method = "session"
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            verify_csrf()
    tenant = current_app.extensions["database"].tenant(principal.tenant_id)
    g.principal, g.tenant = principal, tenant
    enforce_limit(
        current_app.extensions["redis"], "principal",
        f"{principal.tenant_id}:{principal.subject}", settings.rate_limit_per_minute,
    )


@auth.get("/auth/login")
def login():
    identity = current_app.extensions["identity"]
    identity.metadata()
    session.clear()
    session["login_started_at"] = time.time()
    current_app.session_interface.regenerate(session)
    try:
        return identity.client.authorize_redirect(f"{identity.settings.public_url}/auth/callback")
    except (OAuthError, RequestException) as error:
        raise APIError(503, "identity_unavailable", "Unable to start sign-in. Try again shortly.") from error


@auth.get("/auth/callback")
def callback():
    identity = current_app.extensions["identity"]
    identity.metadata()
    if time.time() - session.get("login_started_at", 0) > 600:
        raise APIError(401, "login_expired", "This sign-in attempt expired. Start sign-in again.")
    state_data = identity.client.framework.get_state_data(session, request.args.get("state"))
    if not isinstance(state_data, dict) or not isinstance(state_data.get("nonce"), str):
        raise APIError(401, "login_failed", "This sign-in attempt is invalid. Start sign-in again.")
    try:
        token = identity.client.authorize_access_token(
            claims_options={"iss": {"essential": True, "value": identity.settings.oidc_issuer}},
            leeway=30,
        )
        principal = identity.verify(
            token.get("id_token"), audience=identity.settings.oidc_client_id, expected_nonce=state_data["nonce"],
        )
    except (OAuthError, JoseError, RequestException) as error:
        session.clear()
        raise APIError(401, "login_failed", "Sign-in could not be verified. Start sign-in again.") from error
    database = current_app.extensions["database"]
    database.tenant(principal.tenant_id)
    expires_at = min(principal.expires_at, int(time.time()) + identity.settings.session_lifetime_seconds)
    session.clear()
    session["principal"] = {**principal.to_dict(), "expires_at": expires_at}
    session["csrf_token"] = secrets.token_urlsafe(32)
    session.permanent = True
    current_app.session_interface.regenerate(session)
    with database.session(principal.tenant_id) as db:
        audit(db, principal.tenant_id, principal.subject, "signed_in", None, g.request_id)
    return redirect("/")


@auth.post("/auth/logout")
def logout():
    verify_csrf()
    session.clear()
    return {"status": "signed_out"}
