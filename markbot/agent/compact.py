"""Conversation context compression service.

Reference: MarkBot compact service
- Summarizes conversation history to reduce token usage
- Preserves important context and technical details
- Supports partial compression (keep recent messages)
"""

import re
from typing import Any, Optional
from loguru import logger


class ConversationCompactor:
    """Handles conversation context compression to manage token limits.
    """
    
    MAX_COMPACT_OUTPUT_TOKENS = 4000
    RECENT_MESSAGES_TO_KEEP = 5
    
    COMPACT_PROMPT = """CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.

Your task is to create a detailed summary of the conversation so far, paying close attention to the user's explicit requests and your previous actions.
This summary should be thorough in capturing technical details, code patterns, and architectural decisions that would be essential for continuing development work without losing context.

Before providing your final summary, wrap your analysis in <analysis> tags to organize your thoughts and ensure you've covered all necessary points. In your analysis process:

1. Chronologically analyze each message and section of the conversation. For each section thoroughly identify:
   - The user's explicit requests and intents
   - Your approach to addressing the user's requests
   - Key decisions, technical concepts and code patterns
   - Specific details like:
     - file names
     - full code snippets
     - function signatures
     - file edits
   - Errors that you ran into and how you fixed them
   - Pay special attention to specific user feedback that you received, especially if the user told you to do something differently.
2. Double-check for technical accuracy and completeness, addressing each required element thoroughly.

Your summary should include the following sections:

1. Primary Request and Intent: Capture all of the user's explicit requests and intents in detail
2. Key Technical Concepts: List all important technical concepts, technologies, and frameworks discussed.
3. Files and Code Sections: Enumerate specific files and code sections examined, modified, or created. Pay special attention to the most recent messages and include full code snippets where applicable and include a summary of why this file read or edit is important.
4. Errors and fixes: List all errors that you ran into, and how you fixed them. Pay special attention to specific user feedback that you received, especially if the user told you to do something differently.
5. Problem Solving: Document problems solved and any ongoing troubleshooting efforts.
6. All user messages: List ALL user messages that are not tool results. These are critical for understanding the users' feedback and changing intent.
7. Pending Tasks: Outline any pending tasks that you have explicitly been asked to work on.
8. Current Work: Describe in detail precisely what was being worked on immediately before this summary request, paying special attention to the most recent messages from both user and assistant. Include file names and code snippets where applicable.
9. Optional Next Step: List the next step that you will take that is related to the most recent work you were doing. IMPORTANT: ensure that this step is DIRECTLY in line with the user's most recent explicit requests, and the task you were working on immediately before this summary request. If your last task was concluded, then only list next steps if they are explicitly in line with the users request. Do not start on tangential requests or really old requests that were already completed without confirming with the user first.
                       If there is a next step, include direct quotes from the most recent conversation showing exactly what task you were working on and where you left off. This should be verbatim to ensure there's no drift in task interpretation.

Here's an example of how your output should be structured:

<example>
<analysis>
[Your thought process, ensuring all points are covered thoroughly and accurately]
</analysis>

<summary>
1. Primary Request and Intent:
   [Detailed description]

2. Key Technical Concepts:
   - [Concept 1]
   - [Concept 2]
   - [...]

</summary>
</example>

Please provide your summary based on the conversation so far, following this structure and ensuring precision and thoroughness in your response. 

REMINDER: Do NOT call any tools. Respond with plain text only — an <analysis> block followed by a <summary> block. Tool calls will be rejected and you will fail the task.
"""
    
    def __init__(self, llm_client: Any):
        """Initialize the compactor with an LLM client.
        
        Args:
            llm_client: LLM client for generating summaries
        """
        self.llm_client = llm_client
    
    def should_compact(
        self, 
        messages: list[dict[str, Any]], 
        current_tokens: int,
        max_tokens: int,
        threshold: float = 0.8
    ) -> bool:
        """Determine if conversation should be compacted.
        
        Args:
            messages: Conversation messages
            current_tokens: Current token count
            max_tokens: Maximum allowed tokens
            threshold: Threshold ratio to trigger compaction
            
        Returns:
            True if compaction is needed
        """
        if current_tokens > max_tokens * threshold:
            if len(messages) > self.RECENT_MESSAGES_TO_KEEP * 2:
                return True
        return False
    
    def compact_conversation(
        self,
        messages: list[dict[str, Any]],
        keep_recent: int = 5
    ) -> tuple[str, list[dict[str, Any]]]:
        """Compact conversation by summarizing old messages.
        
        Args:
            messages: Full conversation history
            keep_recent: Number of recent messages to keep
            
        Returns:
            Tuple of (summary, recent_messages)
        """
        if len(messages) <= keep_recent * 2:
            return "", messages
        
        messages_to_compact = messages[:-keep_recent * 2]
        recent_messages = messages[-keep_recent * 2:]
        
        conversation_text = self._format_messages_for_compact(messages_to_compact)
        
        summary = self._generate_summary(conversation_text)
        
        formatted_summary = self._format_summary(summary)
        
        return formatted_summary, recent_messages
    
    def _format_messages_for_compact(self, messages: list[dict[str, Any]]) -> str:
        """Format messages for compact prompt.
        
        Args:
            messages: Messages to format
            
        Returns:
            Formatted conversation text
        """
        lines = []
        for msg in messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            
            if isinstance(content, list):
                text_parts = []
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            text_parts.append(block.get("text", ""))
                        elif block.get("type") == "image":
                            text_parts.append("[image]")
                        elif block.get("type") == "tool_use":
                            tool_name = block.get("name", "unknown")
                            text_parts.append(f"[tool_use: {tool_name}]")
                        elif block.get("type") == "tool_result":
                            text_parts.append("[tool_result]")
                content = "\n".join(text_parts)
            
            lines.append(f"{role.upper()}: {content}")
        
        return "\n\n".join(lines)
    
    def _generate_summary(self, conversation_text: str) -> str:
        """Generate summary using LLM.
        
        Args:
            conversation_text: Formatted conversation text
            
        Returns:
            Generated summary
        """
        if not self.llm_client:
            logger.warning("No LLM client available for compaction, using simple summary")
            lines = conversation_text.split("\n\n")[:5]
            return f"[Simple summary - first 5 messages]\n" + "\n\n".join(lines)
        
        try:
            response = self.llm_client.chat.completions.create(
                model=self.llm_client.model,
                messages=[
                    {"role": "system", "content": self.COMPACT_PROMPT},
                    {"role": "user", "content": conversation_text}
                ],
                max_tokens=self.MAX_COMPACT_OUTPUT_TOKENS,
                temperature=0.3
            )
            
            return response.choices[0].message.content
        except Exception as e:
            logger.error(f"Failed to generate compact summary: {e}")
            return f"[Compaction failed: {e}]"
    
    def _format_summary(self, summary: str) -> str:
        """Format the summary by removing analysis tags.
        
        Args:
            summary: Raw summary with analysis tags
            
        Returns:
            Formatted summary
        """
        formatted = summary
        
        formatted = re.sub(
            r'<analysis>[\s\S]*?</analysis>',
            '',
            formatted
        )
        
        summary_match = re.search(r'<summary>([\s\S]*?)</summary>', formatted)
        if summary_match:
            content = summary_match.group(1) or ''
            formatted = re.sub(
                r'<summary>[\s\S]*?</summary>',
                f'Summary:\n{content.strip()}',
                formatted
            )
        
        formatted = re.sub(r'\n\n+', '\n\n', formatted)
        
        return formatted.strip()
    
    def create_compact_message(
        self,
        summary: str,
        recent_messages_preserved: bool = True
    ) -> dict[str, Any]:
        """Create a system message with the compact summary.
        
        Args:
            summary: The compact summary
            recent_messages_preserved: Whether recent messages are preserved
            
        Returns:
            System message dict
        """
        content = f"""This session is being continued from a previous conversation that ran out of context. The summary below covers the earlier portion of the conversation.

{summary}"""
        
        if recent_messages_preserved:
            content += "\n\nRecent messages are preserved verbatim."
        
        content += "\n\nContinue the conversation from where it left off without asking the user any further questions. Resume directly — do not acknowledge the summary, do not recap what was happening, do not preface with \"I'll continue\" or similar. Pick up the last task as if the break never happened."
        
        return {
            "role": "system",
            "content": content
        }
