"""
Conversation-aware extraction prompts.

Based on Mem0's three-prompt paradigm, adapted for Remembra.
These prompts handle:
1. Fact extraction from multi-turn conversations
2. Speaker attribution and pronoun resolution
3. Importance scoring for long-term value
4. Deduplication decisions via function calling
"""

# ============================================================================
# Conversation Extraction Prompts
# ============================================================================

CONVERSATION_EXTRACTION_SYSTEM_PROMPT = """You are a Personal Information Organizer specialized in extracting memorable facts from conversations. Your job is to identify information worth remembering long-term.

EXTRACTION RULES:
1. Extract facts from BOTH user and assistant messages (unless told otherwise)
2. Attribute each fact to the correct speaker by name
3. Resolve pronouns using conversation context (he/she/they → actual names)
4. Convert relative times to absolute if timestamps are provided
5. Prioritize extraction by value:
   - HIGHEST: Life events, relationships, strong preferences, medical info
   - HIGH: Plans, goals, professional info, locations
   - MEDIUM: Casual preferences, interests, routine activities
   - LOW: Transient info, greetings, pleasantries (filter these out)

6. Score each fact's importance 0.0-1.0:
   - 0.9-1.0: Life events, relationships, strong preferences, medical info
   - 0.7-0.8: Plans, goals, professional info, locations  
   - 0.5-0.6: Casual preferences, interests, routine activities
   - 0.0-0.4: Transient info (should be filtered)

7. Each fact must be:
   - Atomic: One piece of information per fact
   - Self-contained: Understandable without context
   - Attributed: Include the speaker's name (e.g., "Mani prefers..." not "User prefers...")

8. NEVER extract facts from system messages
9. Detect the language of user input and record facts in that language
10. If no name is provided, use the role (User, Assistant)

DO NOT EXTRACT:
- Greetings, filler words, pleasantries
- Questions without actionable information
- Acknowledgments like "ok", "thanks", "got it"
- Information already explicitly stated in a previous fact

OUTPUT FORMAT:
Return a JSON object with a "facts" array. Each fact has:
- content: The atomic fact statement
- importance: Float 0.0-1.0
- speaker: Name or role of who stated this
- source_message: Index of the message (0-based)

Example:
{"facts": [
  {"content": "Mani's wife is named Suzan", "importance": 0.9, "speaker": "Mani", "source_message": 2},
  {"content": "Mani prefers dark mode for all applications", "importance": 0.7, "speaker": "Mani", "source_message": 4}
]}

If no memorable facts exist, return: {"facts": []}"""


CONVERSATION_EXTRACTION_USER_PROMPT = """Extract all memorable facts from this conversation.

CONVERSATION:
{formatted_messages}

{context_section}

EXTRACTION SETTINGS:
- Extract from: {extract_from} messages
- Minimum importance: {min_importance}

Return JSON with "facts" array. Each fact needs: content, importance (0.0-1.0), speaker, source_message (index)."""


# ============================================================================
# Deduplication Decision Prompts
# ============================================================================

DEDUP_DECISION_PROMPT = """You are deciding how to integrate a new fact with existing memories.

NEW FACT: {new_fact}

EXISTING SIMILAR MEMORIES:
{existing_memories}

Your task: Decide ONE action for how to handle this new fact.

ACTIONS:
- ADD: This is genuinely new information not covered by existing memories
- UPDATE: This augments or corrects an existing memory (ALWAYS keep MORE information when merging)
- DELETE: This directly contradicts an existing memory and the new info is more recent/reliable
- NOOP: This is already captured in existing memories with equal or more detail

RULES:
- Prefer UPDATE over DELETE when information can be merged
- When updating, preserve all relevant details from both old and new
- Only use DELETE for direct contradictions (e.g., job change, status change)
- Use NOOP if the new fact adds nothing to existing memories

Call the decide_action function with your decision."""


# OpenAI function calling schema for dedup decisions
DEDUP_DECISION_FUNCTIONS = [
    {
        "type": "function",
        "function": {
            "name": "decide_action",
            "description": "Decide how to handle a new fact relative to existing memories",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["ADD", "UPDATE", "DELETE", "NOOP"],
                        "description": "The action to take for this fact",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Brief explanation of why this action was chosen",
                    },
                    "target_memory_id": {
                        "type": "string",
                        "description": "For UPDATE/DELETE: the ID of the existing memory to modify. Null for ADD/NOOP.",
                    },
                    "merged_content": {
                        "type": "string",
                        "description": "For UPDATE: the combined fact merging old and new information. Null for other actions.",
                    },
                },
                "required": ["action", "reason"],
            },
        },
    }
]


# ============================================================================
# Helper Functions
# ============================================================================

def format_messages_for_extraction(
    messages: list[dict],
    extract_from: str = "both",
) -> str:
    """
    Format messages into a readable transcript for the LLM.
    
    Args:
        messages: List of message dicts with role, content, name, timestamp
        extract_from: 'user', 'assistant', or 'both'
    
    Returns:
        Formatted string transcript
    """
    lines = []
    
    for i, msg in enumerate(messages):
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        name = msg.get("name")
        timestamp = msg.get("timestamp")
        
        # Skip based on extract_from filter
        if extract_from == "user" and role != "user":
            continue
        if extract_from == "assistant" and role != "assistant":
            continue
        
        # Build speaker label
        speaker = name or role.capitalize()
        
        # Add timestamp if available
        time_str = ""
        if timestamp:
            if isinstance(timestamp, str):
                time_str = f" [{timestamp}]"
            else:
                time_str = f" [{timestamp.isoformat()}]"
        
        lines.append(f"[{i}] {speaker}{time_str}: {content}")
    
    return "\n".join(lines)


def format_existing_memories(memories: list[dict]) -> str:
    """
    Format existing memories for the dedup decision prompt.
    
    Args:
        memories: List of memory dicts with id, content, score
    
    Returns:
        Formatted string for LLM context
    """
    if not memories:
        return "No similar existing memories found."
    
    lines = []
    for mem in memories:
        mem_id = mem.get("id", "unknown")
        content = mem.get("content", "")
        score = mem.get("score", 0.0)
        lines.append(f"- ID: {mem_id} (similarity: {score:.2f})\n  Content: {content}")
    
    return "\n".join(lines)
