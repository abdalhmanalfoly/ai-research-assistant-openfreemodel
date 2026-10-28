"""A. Configuration tests: defaults, overrides, validation, secret handling. No network."""
import os
import unittest
from unittest import mock

from app.core.config import Settings, find_obsolete_settings
from app.core.errors import ConfigurationError
from tests.fixtures.openrouter_fakes import CHAT_MODEL, EMBED_MODEL, FAKE_KEY


def S(**kw):
    return Settings(_env_file=None, **kw)


class ConfigTests(unittest.TestCase):
    def test_defaults_are_openrouter_free_models(self):
        s = S()
        self.assertEqual((s.llm_provider, s.llm_model), ("openrouter", CHAT_MODEL))
        self.assertEqual((s.embedding_provider, s.embedding_model, s.embedding_dim),
                         ("openrouter", EMBED_MODEL, 2048))
        self.assertTrue(s.embedding_dim > 2000)  # above the pgvector HNSW limit on purpose (full-scan search)
        self.assertEqual(s.openrouter_base_url, "https://openrouter.ai/api/v1")

    def test_models_and_dimensions_come_from_environment(self):
        env = {"LLM_MODEL": "nvidia/nemotron-3.5-lightning:free",
               "EMBEDDING_MODEL": "openai/text-embedding-3-small", "EMBEDDING_DIMENSIONS": "1536"}
        with mock.patch.dict(os.environ, env):
            s = S()
        self.assertEqual((s.llm_model, s.embedding_model, s.embedding_dim),
                         ("nvidia/nemotron-3.5-lightning:free", "openai/text-embedding-3-small", 1536))

    def test_legacy_embedding_dim_name_still_read(self):
        with mock.patch.dict(os.environ, {"EMBEDDING_DIM": "1024"}):
            self.assertEqual(S().embedding_dim, 1024)

    def test_dimension_validation(self):
        for kw in ({"embedding_dim": 0}, {"embedding_dim": 4097},
                   {"embedding_model": EMBED_MODEL, "embedding_dim": 96},   # below the model's published minimum
                   {"embedding_model": EMBED_MODEL, "embedding_dim": 3072}):  # above its published maximum
            with self.assertRaises(Exception, msg=str(kw)):
                S(**kw)
        self.assertEqual(S(embedding_dim=2048).embedding_dim, 2048)  # the model's default size is accepted

    def test_removed_providers_rejected_with_actionable_message(self):
        for kw, hint in (({"llm_provider": "ollama"}, "LLM_PROVIDER=openrouter"),
                         ({"embedding_provider": "huggingface"}, "EMBEDDING_PROVIDER=openrouter"),
                         ({"llm_provider": "openai"}, "LLM_PROVIDER=openrouter"),
                         ({"embedding_provider": "openai"}, "EMBEDDING_PROVIDER=openrouter")):
            with self.assertRaises(Exception) as ctx:
                S(**kw)
            self.assertIn(hint, str(ctx.exception))

    def test_openrouter_style_model_ids_with_slashes_are_accepted(self):
        self.assertEqual(S(embedding_model="qwen/qwen3-embedding:free").embedding_model, "qwen/qwen3-embedding:free")

    def test_cors_wildcard_rejected(self):
        with self.assertRaises(Exception):
            S(cors_origins="*")
        self.assertEqual(S(cors_origins="http://localhost:5500").cors_origins, "http://localhost:5500")

    # ---- secrets ----
    def test_missing_key_gives_clear_error_and_is_not_required_at_import(self):
        s = S()  # constructing settings must work without a key (Alembic imports it)
        self.assertFalse(s.openrouter_key_configured)
        with self.assertRaises(ConfigurationError) as ctx:
            s.require_openrouter_api_key()
        self.assertIn("OPENROUTER_API_KEY", str(ctx.exception))

    def test_empty_or_blank_key_counts_as_missing(self):
        for v in ("", "   "):
            self.assertFalse(S(openrouter_api_key=v).openrouter_key_configured)

    def test_key_is_never_exposed_by_repr_str_dump_or_diagnostics(self):
        s = S(openrouter_api_key=FAKE_KEY)
        self.assertEqual(s.require_openrouter_api_key(), FAKE_KEY)
        blobs = [repr(s), str(s), s.model_dump_json(), str(s.model_dump()), str(s.diagnostics()),
                 repr(s.openrouter_api_key)]
        for b in blobs:
            self.assertNotIn(FAKE_KEY, b)
            self.assertNotIn("FAKE-TEST-KEY", b)
        self.assertEqual(s.diagnostics()["openrouter_api_key"], "configured")

    def test_key_with_whitespace_rejected_without_echoing_it(self):
        with self.assertRaises(Exception) as ctx:
            S(openrouter_api_key="sk-or-abc def\nghi")
        self.assertNotIn("sk-or-abc", str(ctx.exception))

    def test_base_url_guard_locks_the_key_to_openrouter(self):
        self.assertEqual(S(openrouter_base_url="http://127.0.0.1:9999/v1/").openrouter_base_url,
                         "http://127.0.0.1:9999/v1")  # local dev ok
        for url in ("https://evil.example.com/v1", "https://api.openai.com/v1", "http://openrouter.ai/api/v1",
                    "ftp://x", "not a url"):
            with self.assertRaises(Exception, msg=url):
                S(openrouter_base_url=url)
        S(openrouter_base_url="https://gateway.example.com/v1",
          openrouter_allow_custom_base_url=True)  # explicit opt-in only

    def test_obsolete_local_inference_and_openai_settings_are_reported_by_name_only(self):
        with mock.patch.dict(os.environ, {"OLLAMA_BASE_URL": "http://secret-host:11434", "OPENAI_API_KEY": "sk-x",
                                          "EMBEDDING_DEVICE": "cuda"}):
            found = find_obsolete_settings()
        self.assertIn("OLLAMA_BASE_URL", found)
        self.assertIn("EMBEDDING_DEVICE", found)
        self.assertIn("OPENAI_API_KEY", found)
        self.assertNotIn("secret-host", str(found))

    def test_diagnostics_names_shapes_and_never_the_base_url_secret(self):
        import json
        s = S(openrouter_api_key=FAKE_KEY)
        d = s.diagnostics()
        self.assertEqual((d["llm_provider"], d["llm_model"]), ("openrouter", CHAT_MODEL))
        self.assertEqual(d["embedding_dimension"], 2048)
        self.assertEqual(d["openrouter_host"], "openrouter.ai")
        self.assertNotIn(FAKE_KEY, json.dumps(d))


if __name__ == "__main__":
    unittest.main()
