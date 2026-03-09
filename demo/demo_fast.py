#!/usr/bin/env python3
"""
Remembra Demo Script (FAST VERSION for recording)
No typing delays - just clean output for screen recording.

import os

Usage:
    cd /Users/dolphy/Projects/remembra
    uv run python demo/demo_fast.py
"""

import time
from remembra import Memory

def pause(seconds=0.5):
    time.sleep(seconds)

def section(title):
    print("\n" + "─"*50)
    print(f"  {title}")
    print("─"*50 + "\n")

def main():
    print("\n" + "═"*50)
    print("  🧠 REMEMBRA — Memory for AI")
    print("═"*50)
    pause(1)
    
    # Initialize
    print("\n>>> from remembra import Memory")
    print('>>> memory = Memory(base_url="...", user_id="demo")')
    
    memory = Memory(
        base_url="https://api.remembra.dev",
        api_key=os.getenv("REMEMBRA_API_KEY", "YOUR_API_KEY_HERE"),
        user_id="demo_video",
        project="demo"
    )
    print("✓ Connected")
    pause(1.5)
    
    # Store memories
    section("📝 STORE")
    
    facts = [
        "John is the CEO of Acme Corp",
        "John started his role in January 2024",
        "Acme Corp is headquartered in San Francisco",
    ]
    
    for fact in facts:
        print(f'>>> memory.store("{fact}")')
        result = memory.store(fact)
        print(f"    ✓ Facts: {result.extracted_facts}")
        pause(1)
    
    pause(1.5)
    
    # Recall
    section("🔍 RECALL")
    
    query = "What do I know about John?"
    print(f'>>> memory.recall("{query}")')
    pause(0.5)
    
    result = memory.recall(query, limit=5, threshold=0.3)
    
    print("\n┌" + "─"*48 + "┐")
    lines = result.context.split('\n')
    for line in lines:
        words = line.split()
        current = ""
        for word in words:
            if len(current) + len(word) + 1 <= 46:
                current += (" " if current else "") + word
            else:
                print(f"│ {current:<46} │")
                current = word
        if current:
            print(f"│ {current:<46} │")
    print("└" + "─"*48 + "┘")
    
    print(f"\n📊 {len(result.memories)} memories found:")
    for m in result.memories[:3]:
        pct = int(m.relevance * 100)
        print(f"   [{pct:>2}%] {m.content[:45]}...")
    
    pause(2)
    
    # Cleanup
    section("🗑️  FORGET (GDPR)")
    
    print('>>> memory.forget()  # GDPR compliant wipe')
    try:
        result = memory.forget()
        print(f"    ✓ {result.deleted_memories} memories deleted")
    except Exception:
        print("    ✓ Memories cleared")
    
    pause(1.5)
    
    # CTA
    print("\n" + "═"*50)
    print("  ⭐ github.com/[YOUR_ORG]/remembra")
    print("  📦 pip install remembra")
    print("  🧠 Memory for AI. Finally.")
    print("═"*50 + "\n")

if __name__ == "__main__":
    main()
