"""
Standalone test script — runs the full research pipeline end-to-end.
Tests the research engine with different topics to verify it finds sources.
"""
import asyncio
import sys
import os
import logging

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

from database.models import init_db, get_connection
from database import store
from research.engine import run_research

logger = logging.getLogger(__name__)


async def test_research(test_name: str, answers: dict):
    """Run a single research test and report results."""
    print(f"\n{'='*60}")
    print(f"TEST: {test_name}")
    print(f"{'='*60}")
    print(f"Topic: {answers.get('topic', 'N/A')}")

    # Create stream
    stream_id = store.create_stream(
        user_id=999,  # test user
        name=f"TEST: {answers.get('topic', 'unknown')[:40]}",
        criteria=answers,
    )
    print(f"Stream ID: {stream_id}")

    # Progress callback
    async def progress(msg: str):
        print(f"  >> {msg}")

    # Run research
    state = await run_research(answers, stream_id, progress=progress)

    # Report results
    final_sources = state.get("final_sources", [])
    profile = state.get("profile", {})
    candidates = state.get("candidates", [])
    qualified = state.get("qualified", [])

    print(f"\n--- RESULTS for '{test_name}' ---")
    print(f"  Profile domain: {profile.get('broad_domain', 'N/A')}")
    print(f"  Candidates found: {len(candidates)}")
    print(f"  Qualified sources: {len(qualified)}")
    print(f"  Final validated sources: {len(final_sources)}")

    if final_sources:
        print(f"\n  Sources found:")
        for i, src in enumerate(final_sources, 1):
            print(f"    {i}. {src.get('name', 'Unknown')} "
                  f"(score: {src.get('quality_score', 0)})")
            print(f"       URL: {src['url']}")
            print(f"       Keywords: {', '.join(src.get('specific_keywords', [])[:5])}")
            if src.get('description'):
                print(f"       Desc: {src['description'][:100]}")
    else:
        print("  ❌ NO SOURCES FOUND")

    # Show DB sources
    db_sources = store.get_sources_by_stream(stream_id)
    print(f"\n  Sources in DB: {len(db_sources)}")

    return len(final_sources)


async def main():
    # Initialize database
    init_db()

    # Clean up old test streams
    conn = get_connection()
    conn.execute("DELETE FROM streams WHERE user_id = 999")
    conn.commit()
    conn.close()
    print("Cleaned up old test streams.\n")

    results = {}

    # ── Test 1: Broad crypto news ────────────────────────────────────────
    results["broad_crypto"] = await test_research(
        "Broad Crypto News",
        {
            "topic": "Cryptocurrency news — general coverage of Bitcoin, Ethereum, "
                     "altcoins, DeFi, regulation, and market developments",
            "strictness": "Medium — sources should cover crypto regularly but don't "
                          "need to be exclusively crypto",
            "exclusions": "No price prediction sites, no meme coin shilling, "
                          "no pure trading signal services",
        },
    )

    # ── Test 2: Specific niche topic ─────────────────────────────────────
    results["specific_defi"] = await test_research(
        "Specific: EU DeFi Regulation",
        {
            "topic": "DeFi regulation in the European Union, specifically MiCA "
                     "framework, EU crypto compliance, and regulatory news",
            "strictness": "High — only sources that focus significantly on "
                          "crypto regulation and policy",
            "exclusions": "No pure trading content, no US-focused regulation, "
                          "no general crypto price news",
        },
    )

    # ── Test 3: Non-crypto topic ─────────────────────────────────────────
    results["geopolitics"] = await test_research(
        "Geopolitics News",
        {
            "topic": "Geopolitics news — international relations, conflicts, "
                     "diplomacy, trade wars, and global political developments",
            "strictness": "Medium — sources should cover geopolitics regularly",
            "exclusions": "No domestic US politics, no celebrity gossip, "
                          "no opinion-only sites",
        },
    )

    # ── Summary ──────────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")
    for name, count in results.items():
        status = "✅ PASS" if count >= 3 else "⚠️ LOW" if count >= 1 else "❌ FAIL"
        print(f"  {name}: {count} sources — {status}")

    all_pass = all(c >= 3 for c in results.values())
    if all_pass:
        print("\n✅ ALL TESTS PASSED — MVP workflow is working!")
    else:
        print("\n⚠️ Some tests found fewer than 3 sources. May need tuning.")


if __name__ == "__main__":
    asyncio.run(main())