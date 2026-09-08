import io
import json
import logging
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from pydantic import ValidationError

from app.classifier import AIService
from app.config import Settings
from app.errors import ModelResponseError
from app.ingest import Classification
from app.observability import JSONFormatter
from tests.helpers import ENV, PAYLOAD


class ModelBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.service = AIService(Settings.from_env(ENV), client=self.client)
        self.ticket = SimpleNamespace(**PAYLOAD, category_hint=None)

    def response(self, content, finish_reason="stop"):
        self.client.chat.completions.create.return_value = SimpleNamespace(choices=[
            SimpleNamespace(finish_reason=finish_reason, message=SimpleNamespace(content=content)),
        ])

    def test_schema_validates_model_output_and_preserves_zero_confidence(self):
        for payload in (
            {"category": "admin", "subcategory": "vpn", "confidence": 1},
            {"category": "network", "subcategory": "<script>", "confidence": 1},
            {"category": "network", "subcategory": "vpn", "confidence": 2},
            {"category": "network", "subcategory": "vpn", "confidence": True},
            {"category": "network", "subcategory": "vpn", "confidence": "0.9"},
            {"category": "network", "subcategory": "vpn", "confidence": float("nan")},
        ):
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                Classification.model_validate(payload)
        self.response('{"category":"network","subcategory":"vpn_timeout","confidence":0}')
        self.assertEqual(self.service.classify(self.ticket).confidence, 0)
        messages = self.client.chat.completions.create.call_args.kwargs["messages"]
        self.assertEqual(messages[0]["role"], "system")
        self.assertNotIn(PAYLOAD["description"], messages[0]["content"])
        self.assertNotIn(PAYLOAD["submitter"], json.dumps(messages))

    def test_malformed_and_truncated_model_json_is_not_a_success(self):
        for content, finish in (("not json", "stop"), ("{}", "stop"), (None, "stop"), ("{}", "length")):
            self.response(content, finish)
            with self.assertRaises(ModelResponseError):
                self.service.classify(self.ticket)

    def test_embedding_dimension_finite_and_nonzero_constraints(self):
        for vector in ([1] * 10, [0.0] * 1536, [float("nan")] * 1536, [True] * 1536):
            self.client.embeddings.create.return_value = SimpleNamespace(data=[SimpleNamespace(embedding=vector)])
            with self.assertRaises(ModelResponseError):
                self.service.embed("runbook content")
        expected = [1.0] + [0.0] * 1535
        self.client.embeddings.create.return_value = SimpleNamespace(data=[SimpleNamespace(embedding=expected)])
        self.assertEqual(self.service.embed("runbook content"), expected)

    def test_structured_exception_logs_do_not_include_secret_messages(self):
        output = io.StringIO()
        handler = logging.StreamHandler(output)
        handler.setFormatter(JSONFormatter())
        logger = logging.Logger("test-redaction")
        logger.addHandler(handler)
        try:
            raise ValueError("password=private-secret; employee-ticket-text")
        except ValueError:
            logger.exception("operation_failed", extra={"request_id": "correlation", "tenant_id": "company-a"})
        result = output.getvalue()
        self.assertNotIn("private-secret", result)
        self.assertNotIn("employee-ticket-text", result)
        record = json.loads(result)
        self.assertEqual(record["event"], "operation_failed")
        self.assertEqual(record["request_id"], "correlation")
        self.assertEqual(record["error_type"], "ValueError")
        self.assertTrue(record["stack"])
