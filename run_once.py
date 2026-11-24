"""CLI: python3 run_once.py [--dry-run] [--force] — one pass without the HTTP server."""
import json
import sys

from agent import pipeline

if __name__ == "__main__":
    result = pipeline.run(dry_run="--dry-run" in sys.argv, force="--force" in sys.argv)
    print(result.get("briefing") or result.get("skipped"))
    print()
    print(json.dumps({k: v for k, v in result.items() if k != "briefing"}, indent=2))
