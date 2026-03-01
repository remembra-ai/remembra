# Chatbot Memory

Building a chatbot that remembers everything.

## The Problem

Standard chatbots have no memory between sessions:

```
Session 1:
User: "My name is Alex and I love hiking"
Bot: "Nice to meet you, Alex!"

Session 2:
User: "What's my name?"
Bot: "I don't know your name." 😞
```

## The Solution

Add Remembra:

```
Session 1:
User: "My name is Alex and I love hiking"
Bot: "Nice to meet you, Alex!" [stores: Alex loves hiking]

Session 2:
User: "What's my name?"
Bot: "Your name is Alex! Last time you mentioned you love hiking." 🎉
```

## Implementation

### Basic Chatbot

```python
from remembra import Memory
import openai

class MemoryBot:
    def __init__(self, user_id: str):
        self.memory = Memory(
            base_url="http://localhost:8787",
            user_id=user_id,
            project="chatbot"
        )
        self.client = openai.OpenAI()
    
    def chat(self, user_message: str) -> str:
        # 1. Recall relevant memories
        context = self.memory.recall(user_message, limit=5)
        
        # 2. Build prompt with memory context
        system_prompt = f"""You are a helpful assistant with memory.

What you remember about this user:
{context if context else "Nothing yet - this is a new user."}

Use this context naturally in conversation. Don't force it."""

        # 3. Generate response
        response = self.client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message}
            ]
        )
        assistant_message = response.choices[0].message.content
        
        # 4. Store the conversation
        self.memory.store(f"User said: {user_message}")
        self.memory.store(f"Assistant responded about: {user_message[:50]}")
        
        return assistant_message

# Usage
bot = MemoryBot(user_id="user_123")
print(bot.chat("My name is Alex and I love hiking"))
print(bot.chat("What do you know about me?"))
```

### With Conversation History

```python
class MemoryBotWithHistory:
    def __init__(self, user_id: str):
        self.memory = Memory(
            base_url="http://localhost:8787",
            user_id=user_id
        )
        self.client = openai.OpenAI()
        self.conversation = []
    
    def chat(self, user_message: str) -> str:
        # Recall long-term context
        context = self.memory.recall(user_message, limit=5, max_tokens=1000)
        
        # Add to conversation history
        self.conversation.append({"role": "user", "content": user_message})
        
        # Build messages
        messages = [
            {
                "role": "system",
                "content": f"""You are a helpful assistant.

Long-term memory (from previous sessions):
{context if context else "Nothing yet."}

Be natural. Don't repeat what they just told you."""
            },
            *self.conversation[-10:]  # Last 10 messages for short-term
        ]
        
        # Generate
        response = self.client.chat.completions.create(
            model="gpt-4o",
            messages=messages
        )
        assistant_message = response.choices[0].message.content
        
        # Add to conversation
        self.conversation.append({
            "role": "assistant",
            "content": assistant_message
        })
        
        # Store important facts (not every message)
        self._maybe_store(user_message)
        
        return assistant_message
    
    def _maybe_store(self, message: str):
        """Only store messages with useful facts."""
        # Simple heuristic: longer messages with "I" or personal info
        if len(message) > 20 and any(word in message.lower() 
            for word in ["i ", "my ", "i'm", "i've", "name", "like", "prefer"]):
            self.memory.store(message)
```

### Production-Ready Version

```python
from remembra import Memory
import openai
import logging

class ProductionChatbot:
    def __init__(
        self,
        user_id: str,
        api_key: str = None,
        base_url: str = "http://localhost:8787"
    ):
        self.memory = Memory(
            base_url=base_url,
            user_id=user_id,
            api_key=api_key,
            project="chatbot_prod"
        )
        self.client = openai.OpenAI()
        self.logger = logging.getLogger(__name__)
    
    def chat(self, user_message: str, session_id: str = None) -> str:
        try:
            # Recall with error handling
            context = self._safe_recall(user_message)
            
            # Generate response
            response = self._generate(user_message, context)
            
            # Store with metadata
            self._safe_store(user_message, session_id)
            
            return response
            
        except Exception as e:
            self.logger.error(f"Chat error: {e}")
            return "I'm having trouble right now. Please try again."
    
    def _safe_recall(self, query: str) -> str:
        try:
            return self.memory.recall(
                query,
                limit=5,
                max_tokens=1500,
                enable_hybrid=True
            )
        except Exception as e:
            self.logger.warning(f"Recall failed: {e}")
            return ""
    
    def _safe_store(self, message: str, session_id: str = None):
        try:
            self.memory.store(
                message,
                metadata={
                    "type": "user_message",
                    "session_id": session_id
                },
                ttl="365d"  # Keep for a year
            )
        except Exception as e:
            self.logger.warning(f"Store failed: {e}")
    
    def _generate(self, message: str, context: str) -> str:
        system = f"""You are a helpful assistant with perfect memory.

Context from previous conversations:
{context if context else "No prior context."}

Guidelines:
- Use context naturally, don't force it
- If user repeats info you know, acknowledge briefly
- Be conversational, not robotic"""

        response = self.client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": message}
            ],
            temperature=0.7
        )
        return response.choices[0].message.content
    
    def forget_user(self):
        """GDPR: Delete all user data."""
        self.memory.forget(all=True)
```

## Best Practices

### 1. Don't Store Everything

```python
# ❌ Bad: Stores noise
memory.store(f"User: {msg}\nBot: {response}")

# ✅ Good: Stores facts
if contains_personal_info(msg):
    memory.store(msg)
```

### 2. Use TTL for Session Context

```python
# Permanent facts
memory.store("User's name is Alex")

# Session context (expires)
memory.store("Currently helping with Python code", ttl="24h")
```

### 3. Handle Missing Context

```python
context = memory.recall(query)
if not context:
    prompt = "You're meeting this user for the first time."
else:
    prompt = f"You remember: {context}"
```

### 4. Don't Repeat Back

```python
# ❌ Bad
"You mentioned you're Alex and love hiking. How can I help you, Alex?"

# ✅ Good
"Hey Alex! Planning any hikes this weekend?"
```

## Example Conversation

```
Session 1:
User: "Hi! My name is Alex and I'm a software engineer at Google."
Bot: "Nice to meet you, Alex! What kind of engineering do you do at Google?"
User: "I work on the Search team, mostly ML stuff."
Bot: "ML on Search - that's fascinating work! What brings you here today?"

[Days later - Session 2]
User: "Hey, can you help me with some Python?"
Bot: "Of course! Working on something for the Search team, or a personal project?"
User: "Wait, you remember where I work?"
Bot: "Yep! You mentioned you're on the Search team doing ML work. 
     It's handy for context when helping with code. What are you working on?"
```
