import argparse
import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    load_dotenv = None

from src.core.local_reasoner_prewarm import prewarm_local_reasoner_from_web


async def _run(max_queries: int, results_per_query: int) -> int:
    report = await prewarm_local_reasoner_from_web(
        max_queries=max_queries,
        results_per_query=results_per_query,
    )
    print("Local reasoner prewarm report:")
    print(f"- state_key={report.get('state_key')}")
    print(f"- queries={report.get('queries')}")
    print(f"- results_seen={report.get('results_seen')}")
    print(f"- summaries_saved={report.get('summaries_saved')}")
    print(f"- aliases_added={report.get('aliases_added')}")
    return 0


def main() -> int:
    if load_dotenv is not None:
        load_dotenv()

    parser = argparse.ArgumentParser(
        description="Prewarm local reasoner with web search/scraping for better cold-start UX."
    )
    parser.add_argument("--max-queries", type=int, default=6, help="How many seed queries to run")
    parser.add_argument("--results-per-query", type=int, default=4, help="Results fetched per query")
    args = parser.parse_args()

    return asyncio.run(_run(args.max_queries, args.results_per_query))


if __name__ == "__main__":
    raise SystemExit(main())
