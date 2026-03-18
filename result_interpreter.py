"""
Universal Result Interpreter - Universal Post-Execution Intelligence

This module normalizes raw TinyFish results and uses Gemini to interpret them
into natural, human-readable summaries.

Key principles:
- Universal, works for any result type
- No portal-specific hardcoding
- Factual accuracy - no hallucination
- Conversational and concise
"""

import json
from datetime import datetime
from google import genai
from google.genai import types
from extractors import GEMINI_MODEL


def normalize_execution_result(parsed_result, session):
    """
    Convert raw TinyFish output into a standard internal shape for interpretation.
    
    This is universal normalization - works for schedules, orders, balances, 
    appointments, confirmations, or any arbitrary portal output.
    
    Args:
        parsed_result: Raw TinyFish result dict
        session: Session dict with context
        
    Returns:
        dict with standardized shape:
        {
            "result_type": "list" | "record" | "action_confirmation" | "status" | "error" | "unknown",
            "title": str,
            "summary_hints": [str],
            "items": list,
            "record": dict,
            "raw_data": dict | list | str | None
        }
    """
    status = parsed_result.get('status', 'unknown')
    data = parsed_result.get('data', {})
    message = parsed_result.get('message', '')
    
    # Initialize normalized result
    normalized = {
        "result_type": "unknown",
        "title": None,
        "summary_hints": [],
        "items": [],
        "record": {},
        "raw_data": data
    }
    
    print(f"[Result Normalize] Analyzing result with status={status}, data_type={type(data).__name__}")
    
    # Handle error cases
    if status in ['error', 'timeout']:
        normalized['result_type'] = 'error'
        normalized['title'] = 'Execution Error'
        normalized['summary_hints'] = [message or 'Unknown error occurred']
        print(f"[Result Normalize] result_type=error")
        return normalized
    
    # Handle empty or null data
    if not data or (isinstance(data, dict) and not data) or (isinstance(data, list) and not data):
        # Check if this is an action confirmation with minimal data
        if status == 'complete' and message:
            normalized['result_type'] = 'action_confirmation'
            normalized['title'] = 'Action Completed'
            normalized['summary_hints'] = [message]
            print(f"[Result Normalize] result_type=action_confirmation")
            return normalized
        else:
            normalized['result_type'] = 'status'
            normalized['title'] = 'Task Status'
            normalized['summary_hints'] = [message or 'Task completed with no data returned']
            print(f"[Result Normalize] result_type=status")
            return normalized
    
    # Analyze data structure
    if isinstance(data, list):
        # List of items
        normalized['result_type'] = 'list'
        normalized['items'] = data
        normalized['title'] = f'Found {len(data)} Items'
        
        # Generate generic hints about the list
        if len(data) > 0:
            first_item = data[0]
            if isinstance(first_item, dict):
                # Detect common field patterns (generic, not portal-specific)
                field_hints = _detect_field_patterns(first_item)
                normalized['summary_hints'] = field_hints
        
        print(f"[Result Normalize] result_type=list, items={len(data)}")
        return normalized
    
    elif isinstance(data, dict):
        # Check if this is a wrapper around a list
        for key, value in data.items():
            if isinstance(value, list) and len(value) > 0:
                # Found a list inside the dict
                normalized['result_type'] = 'list'
                normalized['items'] = value
                normalized['title'] = f'Found {len(value)} {key.replace("_", " ").title()}'
                
                if isinstance(value[0], dict):
                    field_hints = _detect_field_patterns(value[0])
                    normalized['summary_hints'] = field_hints
                
                print(f"[Result Normalize] result_type=list (nested), items={len(value)}")
                return normalized
        
        # Single record/object
        normalized['result_type'] = 'record'
        normalized['record'] = data
        normalized['title'] = 'Record Details'
        
        # Generate hints about important fields
        field_hints = _detect_field_patterns(data)
        normalized['summary_hints'] = field_hints
        
        print(f"[Result Normalize] result_type=record, fields={len(data)}")
        return normalized
    
    else:
        # String or other primitive
        normalized['result_type'] = 'status'
        normalized['title'] = 'Result'
        normalized['summary_hints'] = [str(data)]
        print(f"[Result Normalize] result_type=status (primitive)")
        return normalized


def _detect_field_patterns(item_dict):
    """
    Detect generic field patterns in a dict to provide hints for interpretation.
    
    This is NOT portal-specific. It looks for common patterns like:
    - date/time fields
    - name/title fields
    - status/state fields
    - numeric values
    
    Returns list of hint strings.
    """
    hints = []
    
    if not isinstance(item_dict, dict):
        return hints
    
    # Look for date/time patterns (generic)
    date_keywords = ['date', 'time', 'when', 'start', 'end', 'created', 'updated', 'scheduled']
    for key in item_dict.keys():
        key_lower = key.lower()
        if any(kw in key_lower for kw in date_keywords):
            hints.append(f"Contains temporal field: {key}")
            break
    
    # Look for name/title patterns (generic)
    name_keywords = ['name', 'title', 'label', 'description', 'subject']
    for key in item_dict.keys():
        key_lower = key.lower()
        if any(kw in key_lower for kw in name_keywords):
            hints.append(f"Contains identifier field: {key}")
            break
    
    # Look for status patterns (generic)
    status_keywords = ['status', 'state', 'type', 'category']
    for key in item_dict.keys():
        key_lower = key.lower()
        if any(kw in key_lower for kw in status_keywords):
            hints.append(f"Contains status field: {key}")
            break
    
    # Count numeric fields
    numeric_count = sum(1 for v in item_dict.values() if isinstance(v, (int, float)))
    if numeric_count > 0:
        hints.append(f"Contains {numeric_count} numeric fields")
    
    return hints


def interpret_execution_result(user_task, portal_name, normalized_result, client):
    """
    Use Gemini to turn normalized structured output into natural, concise language.
    
    This is universal interpretation - works for any portal/task type.
    
    Args:
        user_task: Original user request
        portal_name: Name of the portal
        normalized_result: Output from normalize_execution_result()
        client: Gemini client
        
    Returns:
        str: Natural language summary
    """
    print(f"[Result Interpret] Calling Gemini for post-execution summary")
    
    result_type = normalized_result.get('result_type')
    
    # Build context for Gemini
    context = {
        "user_task": user_task,
        "portal_name": portal_name,
        "result_type": result_type,
        "title": normalized_result.get('title'),
        "summary_hints": normalized_result.get('summary_hints', [])
    }
    
    # Include data based on type
    if result_type == 'list':
        items = normalized_result.get('items', [])
        # Limit to first 10 items for Gemini to avoid token overflow
        context['item_count'] = len(items)
        context['sample_items'] = items[:10] if len(items) > 10 else items
        if len(items) > 10:
            context['note'] = f"Showing first 10 of {len(items)} items"
    elif result_type == 'record':
        context['record'] = normalized_result.get('record', {})
    elif result_type == 'error':
        context['error_message'] = normalized_result.get('summary_hints', ['Unknown error'])[0]
    else:
        context['raw_data'] = normalized_result.get('raw_data')
    
    # Build Gemini prompt
    prompt = f"""You are interpreting the result of a web automation task for a user.

CONTEXT:
- User's original request: "{user_task}"
- Portal accessed: {portal_name}
- Result type: {result_type}

RESULT DATA:
{json.dumps(context, indent=2, default=str)}

YOUR TASK:
Generate a clear, natural, human-readable response that explains what was found.

REQUIREMENTS:
1. Start with a concise summary (1-2 sentences)
2. Mention the most important information first
3. If there's a list, summarize it without dumping raw data
4. Stay conversational but professional
5. Be factually accurate - don't invent missing details
6. If result is an error, explain the failure naturally
7. If result is unclear, be cautious and honest about what you can determine
8. Avoid over-formatting - keep it readable
9. Do NOT output raw JSON or Python dict strings
10. Do NOT use markdown formatting like ** or ## - just plain text with line breaks

EXAMPLES OF GOOD RESPONSES:
- "I found 5 upcoming appointments. The next one is on March 20th at 2:00 PM."
- "Your account balance is $1,234.56 as of today."
- "I successfully created the discount code. It's now active in your store."
- "I couldn't complete the login - the portal is asking for a security code that wasn't provided."

Generate your response now (plain text only, no markdown):"""
    
    try:
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                max_output_tokens=2000,
                temperature=0.3
            )
        )
        
        answer = response.text.strip()
        print(f"[Result Interpret] Generated summary ({len(answer)} chars)")
        return answer
        
    except Exception as e:
        print(f"[Result Interpret] Gemini interpretation failed: {e}")
        print(f"[Result Interpret] Falling back to generic formatter")
        return fallback_format_normalized_result(normalized_result)


def fallback_format_normalized_result(normalized_result):
    """
    Fallback formatter if Gemini interpretation fails.
    
    Produces readable output without raw dict dumps.
    
    Args:
        normalized_result: Output from normalize_execution_result()
        
    Returns:
        str: Readable formatted text
    """
    result_type = normalized_result.get('result_type')
    title = normalized_result.get('title', 'Result')
    
    if result_type == 'error':
        hints = normalized_result.get('summary_hints', ['Unknown error'])
        return f"The task could not be completed. {hints[0]}"
    
    elif result_type == 'list':
        items = normalized_result.get('items', [])
        count = len(items)
        
        if count == 0:
            return "I found no items."
        elif count == 1:
            return "I found 1 item."
        else:
            # Try to show first few items naturally
            summary = f"I found {count} items."
            
            if items and isinstance(items[0], dict):
                # Try to extract a representative field
                first_item = items[0]
                # Look for name-like fields
                name_field = None
                for key in ['name', 'title', 'label', 'description', 'subject']:
                    if key in first_item:
                        name_field = key
                        break
                
                if name_field and count <= 5:
                    summary += "\n\n"
                    for i, item in enumerate(items[:5], 1):
                        value = item.get(name_field, 'N/A')
                        summary += f"{i}. {value}\n"
            
            return summary
    
    elif result_type == 'record':
        record = normalized_result.get('record', {})
        if not record:
            return "I found the record, but it contains no data."
        
        # Format as simple key-value pairs
        lines = ["I found the following details:"]
        for key, value in list(record.items())[:10]:  # Limit to 10 fields
            # Clean up key
            clean_key = key.replace('_', ' ').title()
            lines.append(f"- {clean_key}: {value}")
        
        if len(record) > 10:
            lines.append(f"... and {len(record) - 10} more fields")
        
        return "\n".join(lines)
    
    elif result_type == 'action_confirmation':
        hints = normalized_result.get('summary_hints', [])
        if hints:
            return f"Done - {hints[0]}"
        return "The action completed successfully."
    
    elif result_type == 'status':
        hints = normalized_result.get('summary_hints', [])
        if hints:
            return hints[0]
        return "Task completed."
    
    else:
        # Unknown type
        return "I completed the task, but the result format is unclear. You may want to check the portal directly."
