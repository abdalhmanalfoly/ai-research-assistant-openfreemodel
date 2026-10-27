"""Runs tests/integration/e2e_openrouter_http.py in a subprocess (fresh process = fresh settings). Needs TEST_DATABASE_URL."""
import os
import subprocess
import sys
import unittest

URL = os.getenv("TEST_DATABASE_URL", "")


@unittest.skipUnless(URL and "test" in URL.rsplit("/", 1)[-1], "needs TEST_DATABASE_URL (database name must contain 'test')")
class E2ETests(unittest.TestCase):
    def test_full_stack_against_local_openai_stand_in(self):
        r = subprocess.run([sys.executable, "-m", "tests.integration.e2e_openrouter_http"], capture_output=True, text=True, timeout=300)
        self.assertIn("E2E OK", r.stdout, r.stdout[-3000:] + r.stderr[-3000:])


if __name__ == "__main__":
    unittest.main()
