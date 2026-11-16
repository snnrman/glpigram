"""Run an explicitly supplied synthetic evaluation set; no bot/GLPI credentials needed."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
from pathlib import Path

from .services.ai_shadow import ClassifierError, OpenAIClassifier, prepare_input


async def evaluate(path: str, model: str, key: str, internal_services: str = "") -> bool:
    cases = json.loads(Path(path).read_text(encoding="utf-8"))
    client = OpenAIClassifier(key, model=model, internal_services=internal_services)
    passed = tokens = 0
    try:
        for case in cases:
            try:
                result = await client.classify(prepare_input(case["title"], case["description"]))
            except ClassifierError as exc:
                print(json.dumps({"id": case["id"], "error": str(exc)}), flush=True)
                continue
            predicted = result.classification.model_dump()
            ok = predicted == case["expected"]
            passed += ok
            tokens += result.input_tokens + result.output_tokens
            print(
                json.dumps(
                    {"id": case["id"], "passed": ok, "predicted": predicted}, ensure_ascii=False
                ),
                flush=True,
            )
    finally:
        await client.close()
    print(json.dumps({"passed": passed, "total": len(cases), "tokens": tokens}), flush=True)
    return passed == len(cases)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cases", help="JSON file of synthetic cases, never a production export")
    parser.add_argument("--model", default="gpt-5-mini-2025-08-07")
    parser.add_argument("--internal-services", default=os.environ.get("AI_INTERNAL_SERVICES", ""))
    args = parser.parse_args()
    key = os.environ.get("OPENAI_API_KEY") or getpass.getpass("OpenAI API key: ")
    raise SystemExit(
        0 if asyncio.run(evaluate(args.cases, args.model, key, args.internal_services)) else 1
    )


if __name__ == "__main__":
    main()
