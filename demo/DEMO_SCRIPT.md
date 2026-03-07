# Remembra Demo Video Script (60 seconds)

## Hook (5 sec)
**Voiceover:** "Your AI forgets everything between sessions. Let's fix that."

## Setup (10 sec)
```bash
pip install remembra
```

```python
from remembra import Memory
memory = Memory(base_url="http://localhost:8787", user_id="demo")
```

**Voiceover:** "Remembra gives your AI persistent memory with semantic search."

## Store (15 sec)
```python
# Store facts naturally
memory.store("John is the CEO of Acme Corp")
memory.store("John started in January 2024")  
memory.store("Acme Corp is based in San Francisco")
```

**Voiceover:** "Just store information in plain English. Remembra extracts entities and facts automatically."

## Recall (20 sec)
```python
# Ask anything
result = memory.recall("What do I know about John?")
print(result.context)
```

**Output:**
```
"John is the CEO of Acme Corp since January 2024. 
 Acme Corp is based in San Francisco."
```

**Voiceover:** "Semantic search finds relevant memories and synthesizes context. No keywords needed."

## MCP Integration (5 sec)
Show Claude Desktop with Remembra MCP connected

**Voiceover:** "Works with Claude Desktop via MCP. Your AI finally remembers."

## CTA (5 sec)
- GitHub: github.com/remembra-ai/remembra
- "Star us. Ship memory."

---

## Recording Notes
- Dark terminal theme (Dracula or similar)
- Big font (24pt minimum)
- Smooth typing animation (can use asciinema or screen record)
- Clean, no desktop clutter
- Optional: split screen showing entities being extracted in real-time

## Music
- Lo-fi or minimal electronic
- 60 sec track, fade out on CTA
