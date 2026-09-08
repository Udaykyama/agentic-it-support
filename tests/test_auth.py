import time
from dataclasses import replace
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt import PyJWKClient

from app.auth import IdentityProvider
from app.errors import APIError
from tests.helpers import AppTestCase


class IdentityTests(AppTestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.public_jwk = {**jwt.algorithms.RSAAlgorithm.to_jwk(cls.key.public_key(), as_dict=True), "kid": "trusted-key", "use": "sig", "alg": "RS256"}

    def setUp(self):
        super().setUp()
        self.identity = IdentityProvider(self.app, self.settings)
        self.identity.client.server_metadata.update({
            "_loaded_at": time.time(), "issuer": self.settings.oidc_issuer,
            "authorization_endpoint": "https://identity.example/authorize",
            "token_endpoint": "https://identity.example/token",
            "jwks_uri": "https://identity.example/jwks",
            "id_token_signing_alg_values_supported": ["RS256"],
            "jwks": {"keys": [self.public_jwk]},
        })
        self.identity.jwks = PyJWKClient("https://identity.example/jwks")
        self.identity.jwks.fetch_data = Mock(return_value={"keys": [self.public_jwk]})
        self.app.extensions["identity"] = self.identity

    def token(self, *, audience=None, **claims):
        now = int(time.time())
        payload = {
            "iss": self.settings.oidc_issuer, "aud": audience or self.settings.oidc_audience,
            "sub": "alice", "iat": now, "exp": now + 300,
            "tenant_id": "company-a", "roles": ["agent"], "email": "alice@company.example",
            **claims,
        }
        return jwt.encode(payload, self.key, algorithm="RS256", headers={"kid": "trusted-key"})

    def test_valid_signature_and_required_claims(self):
        principal = self.identity.verify(self.token())
        self.assertEqual(principal.tenant_id, "company-a")
        self.assertEqual(principal.roles, ("agent",))
        valid = self.client.get("/api/v1/me", headers={"Authorization": f"Bearer {self.token()}"})
        self.assertEqual(valid.status_code, 200)

    def test_wrong_issuer_audience_expiry_roles_tenant_and_unsigned_tokens(self):
        for change in (
            {"iss": "https://attacker.example"}, {"aud": "different-api"},
            {"exp": int(time.time()) - 100}, {"iat": int(time.time()) + 1000},
            {"tenant_id": ["company-a"]}, {"tenant_id": "../company-b"},
            {"roles": "admin"}, {"roles": []}, {"roles": ["unknown"]}, {"sub": ""},
        ):
            with self.subTest(change=change), self.assertRaises(APIError):
                self.identity.verify(self.token(**change))
        unsigned = jwt.encode({"sub": "alice", "roles": ["admin"]}, "", algorithm="none")
        with self.assertRaises(APIError):
            self.identity.verify(unsigned)
        untrusted_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        wrong = jwt.encode(jwt.decode(self.token(), options={"verify_signature": False}), untrusted_key, algorithm="RS256", headers={"kid": "trusted-key"})
        with self.assertRaises(APIError):
            self.identity.verify(wrong)

    def test_signed_but_unprovisioned_tenant_and_missing_claims(self):
        token = self.token(tenant_id="not-provisioned")
        self.assertEqual(self.client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"}).status_code, 403)
        claims = jwt.decode(self.token(), options={"verify_signature": False})
        for name in ("exp", "iat", "iss", "aud", "sub"):
            missing = {key: value for key, value in claims.items() if key != name}
            token = jwt.encode(missing, self.key, algorithm="RS256", headers={"kid": "trusted-key"})
            with self.subTest(name=name), self.assertRaises(APIError):
                self.identity.verify(token)

    def test_metadata_cannot_change_issuer_and_no_header_selects_tenant(self):
        response = self.client.get("/api/v1/me", headers={
            "Authorization": f"Bearer {self.token()}", "X-Tenant-ID": "company-b",
        })
        self.assertEqual(response.json["tenant"]["id"], "company-a")
        self.identity.client.server_metadata["issuer"] = "https://attacker.example"
        with self.assertRaises(APIError) as result:
            self.identity.verify(self.token())
        self.assertEqual(result.exception.status, 503)

    def test_canonical_slash_terminated_issuer_authenticates(self):
        settings = replace(self.settings, oidc_issuer="https://identity.example/")
        identity = IdentityProvider(self.app, settings)
        identity.client.server_metadata.update({
            **self.identity.client.server_metadata, "issuer": settings.oidc_issuer,
        })
        identity.jwks = self.identity.jwks
        self.assertEqual(identity.client._server_metadata_url, "https://identity.example/.well-known/openid-configuration")
        principal = identity.verify(self.token(iss=settings.oidc_issuer))
        self.assertEqual(principal.tenant_id, "company-a")

    def start_login(self):
        response = self.client.get("/auth/login")
        self.assertEqual(response.status_code, 302)
        parameters = parse_qs(urlsplit(response.location).query)
        self.assertEqual(parameters["code_challenge_method"], ["S256"])
        self.assertEqual(parameters["redirect_uri"], ["http://localhost:8000/auth/callback"])
        self.assertGreater(len(parameters["code_challenge"][0]), 20)
        return parameters

    def test_oidc_pkce_nonce_state_session_rotation_and_replay(self):
        parameters = self.start_login()
        token = self.token(audience=self.settings.oidc_client_id, nonce=parameters["nonce"][0])
        with patch.object(self.identity.client, "fetch_access_token", return_value={"id_token": token, "access_token": "opaque"}):
            response = self.client.get("/auth/callback", query_string={"state": parameters["state"][0], "code": "test-code"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location, "/")
        me = self.client.get("/api/v1/me")
        self.assertEqual(me.status_code, 200)
        self.assertTrue(me.json["csrf_token"])
        self.assertEqual(me.json["tenant"]["id"], "company-a")
        with self.client.session_transaction() as session:
            self.assertNotIn("access_token", session)
            self.assertNotIn("id_token", session)
        replay = self.client.get("/auth/callback", query_string={"state": parameters["state"][0], "code": "test-code"})
        self.assertEqual(replay.status_code, 401)
        response = self.client.post("/auth/logout", headers={"X-CSRF-Token": me.json["csrf_token"]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get("/api/v1/me").status_code, 401)

    def test_oidc_rejects_nonce_mismatch_and_missing_state(self):
        parameters = self.start_login()
        token = self.token(audience=self.settings.oidc_client_id, nonce="wrong")
        with patch.object(self.identity.client, "fetch_access_token", return_value={"id_token": token, "access_token": "opaque"}):
            response = self.client.get("/auth/callback", query_string={"state": parameters["state"][0], "code": "test-code"})
        self.assertEqual(response.status_code, 401)
        parameters = self.start_login()
        with patch.object(self.identity.client, "fetch_access_token") as exchange:
            response = self.client.get("/auth/callback", query_string={"state": "wrong", "code": "test-code"})
            self.assertEqual(response.status_code, 401)
            exchange.assert_not_called()

    def test_nonce_supported_claim_does_not_disable_our_nonce_check(self):
        parameters = self.start_login()
        token = self.token(audience=self.settings.oidc_client_id, nonce="wrong", nonce_supported=False)
        with patch.object(self.identity.client, "fetch_access_token", return_value={"id_token": token, "access_token": "opaque"}):
            response = self.client.get("/auth/callback", query_string={"state": parameters["state"][0], "code": "test-code"})
        self.assertEqual(response.status_code, 401)
