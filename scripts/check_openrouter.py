"""Verify the OpenRouter configuration with REAL requests (the only script besides the app that calls the provider).

  python -m scripts.check_openrouter          # free: both model ids listed in OpenRouter's public catalogs
  python -m scripts.check_openrouter --deep   # also 1 tiny embedding + 1 tiny chat completion on the configured FREE
                                              # models (their published pricing is 0; free tiers have rate limits)

The default check is free and does not authenticate (it proves the model ids exist and are spelled correctly).
--deep proves the key is accepted, both models accept requests and the parameters are valid. It does not print the
key. Exit code 0 only if every check passed.
"""
import argparse
import asyncio
import json
import sys

from app.core.config import get_settings
from app.core.errors import AppError
from app.main import build_embedder
from app.services.provider_check import ProviderCheck
from app.services.providers.openrouter_chat import OpenRouterChatModel


async def run(deep: bool) -> int:
    st = get_settings()
    print(json.dumps(st.diagnostics(), indent=2))
    try:
        llm, emb = OpenRouterChatModel.from_settings(st), build_embedder(st)
    except AppError as e:
        print(f"[{e.code}] {e.message}", file=sys.stderr)
        return 2
    try:
        result = await ProviderCheck(llm, emb, min_interval_seconds=0).run(deep=deep)
    finally:
        await llm.aclose()
        await emb.aclose()
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--deep", action="store_true")
    sys.exit(asyncio.run(run(ap.parse_args().deep)))
