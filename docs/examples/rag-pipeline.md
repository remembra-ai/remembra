# RAG Pipeline

Integrate Remembra into your RAG (Retrieval-Augmented Generation) pipeline.

## Why Remembra + RAG?

Traditional RAG retrieves from static documents. Adding Remembra gives you:

- **User context**: Personalized responses based on user history
- **Session memory**: Remember conversation context
- **Dynamic knowledge**: Store new facts from conversations

## Architecture

```
Query → [ Remembra (user context) ] ─┐
                                      ├──► LLM ──► Response
Query → [ Vector DB (documents) ] ───┘
```

## Implementation

### Basic Integration

```python
from remembra import Memory
from your_rag import DocumentRetriever
import openai

class RAGWithMemory:
    def __init__(self, user_id: str):
        self.memory = Memory(
            base_url="http://localhost:8787",
            user_id=user_id
        )
        self.documents = DocumentRetriever()  # Your existing RAG
        self.client = openai.OpenAI()
    
    def query(self, question: str) -> str:
        # 1. Get user-specific context
        user_context = self.memory.recall(question, limit=3)
        
        # 2. Get document context (traditional RAG)
        doc_context = self.documents.retrieve(question, k=5)
        
        # 3. Build prompt with both
        system = f"""Answer based on the provided context.

User-specific context (their history):
{user_context if user_context else "No user history."}

Documentation context:
{doc_context}

If the user asks something personal, use user context.
If they ask about the product, use documentation.
Combine when relevant."""

        # 4. Generate
        response = self.client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": question}
            ]
        )
        
        answer = response.choices[0].message.content
        
        # 5. Store the interaction
        self.memory.store(f"User asked: {question[:100]}")
        
        return answer
```

### With LangChain

```python
from langchain.memory import BaseMemory
from remembra import Memory

class RemembraMemory(BaseMemory):
    """LangChain-compatible memory using Remembra."""
    
    def __init__(self, user_id: str, **kwargs):
        super().__init__(**kwargs)
        self.memory = Memory(
            base_url="http://localhost:8787",
            user_id=user_id
        )
        self.memory_key = "remembra_context"
    
    @property
    def memory_variables(self) -> list[str]:
        return [self.memory_key]
    
    def load_memory_variables(self, inputs: dict) -> dict:
        query = inputs.get("question", inputs.get("input", ""))
        context = self.memory.recall(query, limit=5)
        return {self.memory_key: context}
    
    def save_context(self, inputs: dict, outputs: dict) -> None:
        user_input = inputs.get("question", inputs.get("input", ""))
        self.memory.store(user_input)
    
    def clear(self) -> None:
        self.memory.forget(all=True)

# Usage with LangChain
from langchain.chains import ConversationalRetrievalChain
from langchain.vectorstores import Chroma
from langchain.llms import ChatOpenAI

chain = ConversationalRetrievalChain.from_llm(
    llm=ChatOpenAI(model="gpt-4o"),
    retriever=chroma_db.as_retriever(),
    memory=RemembraMemory(user_id="user_123")
)
```

### With LlamaIndex

```python
from llama_index.core import VectorStoreIndex
from llama_index.core.memory import BaseMemory
from remembra import Memory

class RemembraLlamaMemory(BaseMemory):
    def __init__(self, user_id: str):
        self.memory = Memory(
            base_url="http://localhost:8787",
            user_id=user_id
        )
    
    def get(self, query: str) -> str:
        return self.memory.recall(query, limit=5)
    
    def put(self, message: str) -> None:
        self.memory.store(message)

# Integrate with your index
memory = RemembraLlamaMemory(user_id="user_123")

def query_with_memory(query: str, index: VectorStoreIndex):
    # Get user context
    user_context = memory.get(query)
    
    # Query index
    query_engine = index.as_query_engine()
    
    # Combine in prompt
    enhanced_query = f"""
    User context: {user_context}
    
    Question: {query}
    """
    
    response = query_engine.query(enhanced_query)
    
    # Store interaction
    memory.put(query)
    
    return response
```

## Advanced Patterns

### Context Window Management

```python
def query_with_budget(question: str, token_budget: int = 3000):
    # Split budget between sources
    user_tokens = token_budget // 3      # 1000 for user context
    doc_tokens = token_budget * 2 // 3   # 2000 for documents
    
    user_context = memory.recall(
        question,
        max_tokens=user_tokens
    )
    
    doc_context = documents.retrieve(
        question,
        max_tokens=doc_tokens
    )
    
    return generate(question, user_context, doc_context)
```

### Hybrid Ranking

```python
def hybrid_retrieve(query: str, k: int = 10):
    # Get memories with scores
    memories = memory.recall_with_scores(query, limit=k)
    
    # Get documents with scores
    docs = documents.retrieve_with_scores(query, k=k)
    
    # Merge and re-rank
    all_results = []
    for m in memories:
        all_results.append({
            "content": m["content"],
            "score": m["score"],
            "source": "memory"
        })
    for d in docs:
        all_results.append({
            "content": d["content"],
            "score": d["score"],
            "source": "document"
        })
    
    # Sort by score
    all_results.sort(key=lambda x: x["score"], reverse=True)
    
    return all_results[:k]
```

### Dynamic Document Learning

```python
def learn_from_conversation(question: str, answer: str, user_id: str):
    """Store verified information as both memory and document."""
    
    # Store in user memory
    memory.store(f"Q: {question}\nA: {answer}")
    
    # If user confirms answer is correct, add to documents
    if user_confirms_correctness:
        documents.add(
            content=answer,
            metadata={"source": "conversation", "user": user_id}
        )
```

## Best Practices

### 1. Separate Concerns

```python
# User-specific context (Remembra)
memory.recall("What plan am I on?")

# General knowledge (Document RAG)
documents.retrieve("How do refunds work?")
```

### 2. Set Appropriate Limits

```python
# User context should be concise
user_context = memory.recall(query, limit=3, max_tokens=500)

# Document context can be longer
doc_context = documents.retrieve(query, k=5, max_tokens=2000)
```

### 3. Use TTL for Session Context

```python
# Ephemeral session context
memory.store("User is asking about pricing", ttl="1h")

# Permanent user facts
memory.store("User's company is Acme Corp")
```

### 4. Don't Duplicate

```python
# ❌ Don't store what's already in documents
memory.store("Our refund policy is 30 days...")

# ✅ Store user-specific facts
memory.store("User requested a refund on order #123")
```
