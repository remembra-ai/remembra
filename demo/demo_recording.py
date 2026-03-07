#!/usr/bin/env python3
"""
Remembra Demo Script
Run this for the 60-second demo video recording.

import os

Usage:
    cd /Users/dolphy/Projects/remembra
    uv run python demo/demo_recording.py
"""

import time
from remembra import Memory

def slow_print(text, delay=0.03):
    """Print text with typing effect."""
    for char in text:
        print(char, end='', flush=True)
        time.sleep(delay)
    print()

def pause(seconds=1):
    time.sleep(seconds)

def main():
    print("\n" + "="*50)
    print("🧠 REMEMBRA DEMO")
    print("="*50 + "\n")
    pause(1)
    
    # Initialize
    slow_print(">>> from remembra import Memory")
    from remembra import Memory
    pause(0.5)
    
    slow_print('>>> memory = Memory(base_url="http://178.156.226.84:8787", api_key="...", user_id="demo")')
    memory = Memory(
        base_url="http://178.156.226.84:8787",
        api_key=os.getenv("REMEMBRA_API_KEY", "YOUR_API_KEY_HERE"),
        user_id="demo_video",
        project="demo"
    )
    print("✓ Connected\n")
    pause(1)
    
    # Store memories
    print("-" * 40)
    print("📝 STORING MEMORIES")
    print("-" * 40 + "\n")
    
    facts = [
        "John is the CEO of Acme Corp",
        "John started his role in January 2024",
        "Acme Corp is headquartered in San Francisco",
        "The company has 150 employees",
    ]
    
    for fact in facts:
        slow_print(f'>>> memory.store("{fact}")')
        result = memory.store(fact)
        print(f"    ✓ Stored | Facts: {result.extracted_facts}")
        pause(0.8)
    
    print()
    pause(1)
    
    # Recall
    print("-" * 40)
    print("🔍 SEMANTIC RECALL")
    print("-" * 40 + "\n")
    
    query = "What do I know about John and his company?"
    slow_print(f'>>> result = memory.recall("{query}")')
    pause(0.5)
    
    result = memory.recall(query, limit=5, threshold=0.3)
    print("\n>>> print(result.context)\n")
    pause(0.5)
    
    print("┌" + "─"*48 + "┐")
    # Word wrap the context
    context = result.context
    words = context.split()
    line = ""
    for word in words:
        if len(line) + len(word) + 1 <= 46:
            line += (" " if line else "") + word
        else:
            print(f"│ {line:<46} │")
            line = word
    if line:
        print(f"│ {line:<46} │")
    print("└" + "─"*48 + "┘")
    
    print(f"\n📊 Found {len(result.memories)} relevant memories")
    for m in result.memories[:3]:
        print(f"    [{m.relevance:.0%}] {m.content[:50]}...")
    
    pause(2)
    
    # Cleanup
    print("\n" + "-" * 40)
    print("🗑️  GDPR COMPLIANT FORGET")
    print("-" * 40 + "\n")
    
    slow_print('>>> memory.forget(user_id="demo_video")')
    result = memory.forget(user_id="demo_video")
    print(f"    ✓ Deleted {result.deleted_memories} memories")
    
    pause(1)
    print("\n" + "="*50)
    print("⭐ github.com/remembra-ai/remembra")
    print("🧠 Memory for AI. Finally.")
    print("="*50 + "\n")

if __name__ == "__main__":
    main()
