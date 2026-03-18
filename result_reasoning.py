"""
Result Reasoning Engine - Conversational Follow-Up Intelligence

This module enables natural follow-up questions about previously retrieved results
without rerunning TinyFish. It uses Gemini to reason over stored result data.

Key principles:
- Universal, works for any result type
- Answers only from available data
- No hallucination of missing fields
- Conversational and concise
"""

import json
from datetime import datetime, timedelta
from extractors import GEMINI_MODEL


def reason_over_previous_result(user_message, session, client):
    """
    Use Gemini to answer follow-up questions about previously stored results.
    
    This enables conversational interaction without rerunning TinyFish:
    - "When is my next shift?" 
    - "Which one is first?"
    - "How many are pending?"
    - "Summarize that"
    
    Args:
        user_message: User's follow-up question
        session: Session dict with last_result stored
        client: Gemini client
        
    Returns:
        str: Natural language answer based on stored data
    """
    print(f"[Reasoning] Gemini answering follow-up question")
    
    # Extract stored result data
    normalized_result = session.get('last_result')
    result_type = session.get('last_result_type')
    previous_summary = session.get('last_result_summary')
    portal_name = session.get('portal_name', 'the portal')
    original_task = session.get('original_task', 'your request')
    
    if not normalized_result:
        print(f"[Reasoning] No stored result found")
        return "I don't have any previous results to reference. Could you ask me to fetch some data first?"
    
    print(f"[Memory] Using stored result type={result_type}")
    
    # Build context for Gemini
    context = {
        "original_task": original_task,
        "portal_name": portal_name,
        "result_type": result_type,
        "previous_summary": previous_summary
    }
    
    # Include relevant data based on type
    if result_type == 'list':
        items = normalized_result.get('items', [])
        context['item_count'] = len(items)
        context['items'] = items  # Include all items for reasoning
    elif result_type == 'record':
        context['record'] = normalized_result.get('record', {})
    elif result_type == 'error':
        context['error_message'] = normalized_result.get('summary_hints', ['Unknown error'])[0]
    else:
        context['raw_data'] = normalized_result.get('raw_data')
    
    # Build Gemini prompt for follow-up reasoning
    prompt = f"""You are continuing a conversation about previously retrieved portal data.

ORIGINAL REQUEST:
"{original_task}"

PORTAL:
{portal_name}

PREVIOUS RESULT SUMMARY:
{previous_summary}

COMPLETE RESULT DATA:
{json.dumps(context, indent=2, default=str)}

USER'S FOLLOW-UP QUESTION:
"{user_message}"

YOUR TASK:
Answer the user's question using ONLY the available data above.

REQUIREMENTS:
1. Be conversational and concise
2. Answer directly - don't repeat the entire summary
3. If the answer requires data not present, say so honestly
4. Do NOT invent or assume missing fields
5. Do NOT use markdown formatting - just plain text
6. If the question is about "next" or "first", use temporal or positional logic
7. If the question is about counting, provide accurate counts
8. If the question is about filtering (e.g., "only confirmed"), apply the filter
9. Stay factual and accurate

EXAMPLES OF GOOD ANSWERS:
- "Your next shift is on March 20th at 8:00 AM."
- "There are 3 pending orders."
- "That's at Rogers Arena."
- "I found 2 confirmed shifts: March 20th and March 21st."
- "I don't have location information in the results."

Generate your answer now (plain text only):"""
    
    try:
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt
        )
        
        answer = response.text.strip()
        print(f"[Reasoning] Generated answer ({len(answer)} chars)")
        return answer
        
    except Exception as e:
        print(f"[Reasoning] Gemini reasoning failed: {e}")
        return "I'm having trouble processing your follow-up question. Could you rephrase it or ask me to fetch fresh data?"


def should_allow_rerun(user_message: str, session: dict) -> bool:
    """
    Determine if user explicitly wants fresh execution.
    Only explicit refresh/update language should allow rerun.
    
    Args:
        user_message: User's message
        session: Session dict
        
    Returns:
        bool: True if explicit rerun requested
    """
    if not user_message:
        return False

    msg = user_message.lower().strip()

    explicit_rerun_phrases = [
        "refresh",
        "refresh it",
        "refresh again",
        "check again",
        "run again",
        "rerun",
        "re-run",
        "update",
        "update it",
        "fetch latest",
        "get latest",
        "latest data",
        "pull again",
        "try again",
        "do it again",
        "run the automation again",
        "check the portal again"
    ]

    for phrase in explicit_rerun_phrases:
        if phrase in msg:
            print(f"[Router] Explicit rerun phrase detected: {phrase}")
            return True

    return False


def looks_like_followup_question(user_message: str) -> bool:
    """
    Detect natural conversational questions about previous data.
    These must NEVER trigger tool execution.
    
    Args:
        user_message: User's message
        
    Returns:
        bool: True if message looks like a follow-up question
    """
    if not user_message:
        return False

    msg = user_message.lower()

    followup_signals = [
        "when",
        "which",
        "what",
        "where",
        "how many",
        "how much",
        "show",
        "tell me",
        "next",
        "first",
        "last",
        "details",
        "timing",
        "time",
        "date",
        "status",
        "summarize",
        "explain",
        "more info",
        "more detail",
        "any other",
        "do i have",
        "is there"
    ]

    return any(signal in msg for signal in followup_signals)


def is_execution_too_recent(session, threshold_seconds=60):
    """
    Check if last tool execution was too recent to warrant automatic rerun.
    
    This prevents accidental reruns from casual messages like:
    - "ok"
    - "thanks"
    - "nice"
    
    Args:
        session: Session dict
        threshold_seconds: Minimum seconds between automatic reruns
        
    Returns:
        bool: True if execution was too recent
    """
    last_run_at = session.get('last_tool_run_at')
    
    if not last_run_at:
        return False
    
    try:
        last_run_time = datetime.fromisoformat(last_run_at)
        elapsed = datetime.now() - last_run_time
        
        if elapsed < timedelta(seconds=threshold_seconds):
            print(f"[Execution Guard] Last run was {elapsed.total_seconds():.0f}s ago (< {threshold_seconds}s threshold)")
            return True
        
        return False
        
    except Exception as e:
        print(f"[Execution Guard] Error checking last run time: {e}")
        return False


def has_stored_result(session):
    """
    Check if session has a stored result available for reasoning.
    
    Args:
        session: Session dict
        
    Returns:
        bool: True if result is available
    """
    return session.get('last_result') is not None
