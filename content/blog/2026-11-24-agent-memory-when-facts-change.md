---
title: When facts change: the part of agent memory that vector search skips
description: Agents fail when an old fact and its replacement both come back. How to store updates so recall returns what is true now, with code you can run.
date: 2026-11-24
tags: [agent memory, vector search, temporal memory, python]
lede: A vector store can tell you which notes look like the question. It cannot tell you which of them is still true. Here is what that costs an agent, and what a memory layer has to do about it.
docs: [guides/temporal, concepts/memory-model, guides/python-sdk, getting-started/quickstart]
---

Most "memory for agents" starts as a vector store: embed every note, embed the question, return the closest notes. That works until a fact changes. Then the old note and the new one are both close to the question, both come back, and the model picks one. Sometimes it picks the old one.

This post shows the failure, the three things a memory layer has to do to prevent it, and a short script you can run against Remembra to watch each one happen. It also shows a case Remembra gets wrong today.

## The failure

Say an agent helps a team that runs a product called Atlas. In March it is told:

> The Atlas API rate limit is 60 requests per minute per API key.

In November the limit changes, and the agent is told:

> The Atlas API rate limit was raised to 120 requests per minute per API key.

A user then asks "What is the Atlas API rate limit?". Both notes are about the rate limit, so both are near the question in embedding space. A plain top-k search returns both. Nothing in the result says which one is current. If the agent is asked to configure a client, half the time it will set the wrong number.

Sorting by date helps for this one question, and breaks others. Most facts an agent holds are old and still true: the database, the review rules, who owns what. Rank by recency and last week's unrelated note starts beating the March note that actually answers the question.

The problem is not ranking. The old note should not be a candidate at all.

## What a memory layer has to do

**Decide at write time.** When a new fact arrives, compare it with the facts already stored and decide what it is: new, a duplicate, or a replacement for one of them. This is where a model earns its cost, because "raised to 120" replacing "is 60" is a judgment about meaning.

**Retire, do not delete.** The replaced fact is marked as superseded and left in place. Deleting it loses the answer to "what did we believe in June, and when did that change?", which is the first thing you want when debugging an agent's past decision.

**Answer as of a time.** Each fact carries the period it was current. A recall can then ask for what was true at a given moment, and get the old fact back when that is the right answer.

## Try it

Install the client and get a key from [app.remembra.dev](https://app.remembra.dev/signup). The client talks to `localhost` unless you give it a URL, so pass both.

```bash
pip install remembra
export REMEMBRA_API_KEY=your-key
```

```python
import os
from remembra import Memory

memory = Memory(
    base_url="https://api.remembra.dev",
    api_key=os.environ["REMEMBRA_API_KEY"],
    project="facts-change-demo",
)

memory.store("The Atlas API rate limit is 60 requests per minute per API key.")
memory.store("Atlas sends transactional email through Postmark.")

result = memory.recall("What is the Atlas API rate limit?", limit=3)
for item in result.memories:
    print(f"{item.relevance:.2f}  {item.content}")
```

```text
0.86  The Atlas API rate limit is 60 requests per minute per API key.
0.20  Atlas sends transactional email through Postmark
```

Note the time, then store the change the way a user would say it. There is no "update" call and no id to look up.

```python
from datetime import UTC, datetime

before_change = datetime.now(UTC).isoformat()

memory.store("The Atlas API rate limit was raised to 120 requests per minute per API key.")

result = memory.recall("What is the Atlas API rate limit?", limit=3)
```

```text
0.86  The Atlas API rate limit was raised to 120 requests per minute per API key.
0.21  Atlas sends transactional email through Postmark
```

The 60 is gone from the answer. It was not deleted. The new memory records what it replaced and why:

```json
{
  "supersedes": "12527c71-acfe-442a-b56f-5d5275837b44",
  "consolidation_reason": "rate limit updated"
}
```

Ask for history and both come back:

```python
memory.recall("What is the Atlas API rate limit?", limit=3, include_superseded=True)
```

```text
0.86  The Atlas API rate limit is 60 requests per minute per API key.
0.86  The Atlas API rate limit was raised to 120 requests per minute per API key.
0.21  Atlas sends transactional email through Postmark
```

And ask as of the moment before the change:

```python
memory.recall("What is the Atlas API rate limit?", limit=3, as_of=before_change)
```

```text
0.86  The Atlas API rate limit is 60 requests per minute per API key.
0.20  Atlas sends transactional email through Postmark
```

These outputs are from Remembra Cloud 0.17.8 on 24 November 2026. The same steps as one script, with cleanup, are in [the repository](https://github.com/remembra-ai/remembra/blob/main/content/blog/examples/agent_memory_when_facts_change.py). Your scores will differ slightly.

## Where it still goes wrong

The decision above is made by a model, and only for stored facts that are close enough to the new one to be candidates. An update that does not look like the fact it replaces can slip past. This one did, in the same session:

```python
memory.store("The primary Atlas contact at Globex is Elena Vasquez.")
memory.store("Raj Patel took over the Globex account last week.")

memory.recall("Who is our contact at Globex?", limit=3)
```

```text
0.86  The primary Atlas contact at Globex is Elena Vasquez.
0.61  Raj Patel took over the Globex account on November 17, 2026
0.15  Atlas sends transactional email through Postmark
```

Both facts are live, and the stale one ranks first. A person reading both would work it out. An agent in a hurry may not. (The server did turn "last week" into a date, which is what lets the second line stand on its own later.)

Three things reduce this today.

**Say what changed.** An update that names the fact it replaces is matched and retired. In a fresh project holding only the Elena Vasquez fact:

```python
memory.store("The primary Atlas contact at Globex changed from Elena Vasquez to Raj Patel.")

memory.recall("Who is our contact at Globex?", limit=3)
```

```text
0.85  The primary Atlas contact at Globex is Raj Patel
```

One fact comes back, and it was stored as the current state, not as the sentence that was sent. Agents that write their own memories can be told to phrase updates this way.

The other two:

- **Replace by id when you know it.** The REST API has `POST /api/v1/memories/{id}/supersede`, which takes the new content and a reason and retires that memory. It is the right call when your application already knows which record is out of date.
- **Check the write.** A replacement shows `supersedes` in the new memory's metadata. If you expected one and it is missing, you have two live facts.

There is one more failure mode to know about. If the model call fails at write time, the new fact is stored next to the old one rather than dropped. You keep the data and lose the retirement.

## What to ask of any memory tool

These questions apply to whatever you use, including one you build yourself.

| Question | Why it matters |
|---|---|
| When a fact is replaced, does the old one stop coming back? | Otherwise recall returns contradictions and the model chooses. |
| Is the old fact kept, and can I ask for it? | You need it to explain what the agent did last month. |
| Can I recall as of a past time? | Audits and debugging both ask "what did it know then?". |
| What happens to a write when the model is down? | Dropped, queued and stored unchecked are three different outages. |
| Can I see why a fact was replaced? | A wrong replacement has to be found before it can be fixed. |

Vector search is one part of the answer to the first row and none of the rest. That gap, more than retrieval quality, is the difference between a search index and a memory.
