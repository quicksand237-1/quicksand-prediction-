import json
import tempfile
import unittest
from pathlib import Path

from ai_provider import AIConfigurationError, AIProvider, _safe_base_url, parse_structured_output


class FakeCredentialStore:
    def __init__(self):
        self.secret = None

    def read(self):
        return self.secret

    def write(self, secret):
        self.secret = secret
        return True

    def delete(self):
        self.secret = None
        return True


class AIProviderTests(unittest.TestCase):
    def test_structured_output_is_whitelisted_and_confidence_bounded(self):
        result = parse_structured_output("""```json
        {"summary":"test","probability":0.99,"events":[{"topic":"Tesla","direction":"bad","confidence":2,"impact_horizon_hours":999,"reason":"x","evidence_document_ids":["doc"]}],"conflicts":["c"]}
        ```""")
        self.assertNotIn("probability", result)
        self.assertEqual(result["events"][0]["direction"], "unknown")
        self.assertEqual(result["events"][0]["confidence"], 0.9)
        self.assertEqual(result["events"][0]["impact_horizon_hours"], 168)

    def test_config_never_writes_key_to_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            provider = AIProvider(Path(directory))
            provider.credential_store = FakeCredentialStore()
            result = provider.configure({
                "api_key": "test-secret-that-must-not-be-in-metadata",
                "provider": "openai",
                "base_url": "https://api.openai.com/v1",
                "model": "test-model",
                "mode": "responses",
            })
            self.assertEqual(result["storage"], "windows_credential_manager")
            metadata = json.loads((Path(directory) / ".ai-config.json").read_text(encoding="utf-8"))
            self.assertNotIn("api_key", metadata)
            self.assertNotIn("test-secret", json.dumps(metadata))

    def test_http_api_is_rejected_except_for_localhost(self):
        with self.assertRaises(AIConfigurationError):
            _safe_base_url("http://remote.example/v1")
        self.assertEqual(_safe_base_url("http://127.0.0.1:9000/v1"), "http://127.0.0.1:9000/v1")

    def test_key_cannot_inject_env_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            provider = AIProvider(Path(directory))
            provider.credential_store = FakeCredentialStore()
            with self.assertRaises(AIConfigurationError):
                provider.configure({"api_key": "key\nINJECTED=1"})


if __name__ == "__main__":
    unittest.main()
