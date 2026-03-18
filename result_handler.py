"""
TinyFish Result Handler for Navi
Processes TinyFish execution results and updates session state
"""

from datetime import datetime
from session_manager import update_session_mode, increment_retry, set_required_fields
from utils import normalize_fields
from result_interpreter import normalize_execution_result, interpret_execution_result, fallback_format_normalized_result


def handle_execution_result(session, parsed_result, saved_nodes, persist_node_func, persist_session_func, client=None):
    """
    Handle TinyFish execution result and update session/node state.
    
    Args:
        session: Session dict
        parsed_result: Parsed TinyFish result dict
        saved_nodes: Dict of saved nodes
        persist_node_func: Function to persist node to DB
        persist_session_func: Function to persist session to DB
    
    Returns:
        {
            "type": "text" | "error",
            "message": str,
            "session_id": str,
            "data": dict | None
        }
    """
    
    session_id = session.get('session_id')
    status = parsed_result.get('status')
    
    print(f"[Result Handler] Processing status={status} for session {session_id[:8]}")
    
    # CASE 1: Complete - success!
    if status == 'complete':
        print(f"[Result Handler] Task completed successfully")
        
        # Update session to complete and clear execution tracking
        update_session_mode(session, 'complete', 'complete')
        session['retry_count'] = 0
        session['execution_started_at'] = None
        persist_session_func(session_id, session)
        
        # Save or update node with credentials
        portal_key = session.get('portal_key')
        matched_node_id = session.get('matched_node_id')
        
        if matched_node_id and matched_node_id in saved_nodes:
            # Update existing node
            node = saved_nodes[matched_node_id]
            node['credentials'] = session.get('credentials', {})
            node['metadata']['last_used'] = datetime.now().isoformat()
            persist_node_func(matched_node_id, node)
            print(f"[Result Handler] Updated node {matched_node_id[:8]} with credentials")
        elif portal_key:
            # Create new node
            import uuid
            node_id = str(uuid.uuid4())
            
            new_node = {
                "id": node_id,
                "portal_key": portal_key,
                "portal_name": session.get('portal_name'),
                "portal_url": session.get('portal_url'),
                "type": session.get('node_type', 'browser'),
                "credentials": session.get('credentials', {}),
                "metadata": {
                    "created_at": datetime.now().isoformat(),
                    "last_used": datetime.now().isoformat()
                }
            }
            
            saved_nodes[node_id] = new_node
            persist_node_func(node_id, new_node)
            print(f"[Result Handler] Created new node {node_id[:8]} (key={portal_key})")
            
            # Store node_id for blueprint storage below
            matched_node_id = node_id
        
        # UNIVERSAL RESULT INTERPRETATION LAYER
        # Step 1: Normalize the raw TinyFish result
        normalized_result = normalize_execution_result(parsed_result, session)
        
        # Step 2: Use Gemini to interpret the result naturally
        user_task = session.get('original_task', 'your request')
        portal_name = session.get('portal_name', 'the portal')
        
        # Use Gemini to interpret result (client passed from caller)
        if client:
            try:
                natural_message = interpret_execution_result(
                    user_task,
                    portal_name,
                    normalized_result,
                    client
                )
            except Exception as e:
                print(f"[Result Handler] Gemini interpretation failed: {e}")
                natural_message = fallback_format_normalized_result(normalized_result)
        else:
            print(f"[Result Handler] No Gemini client provided, using fallback formatter")
            natural_message = fallback_format_normalized_result(normalized_result)
        
        # Prepend success emoji
        message = f"✅ {natural_message}"
        
        # STORE RESULT IN SESSION MEMORY for conversational follow-ups
        session['last_result'] = normalized_result
        session['last_result_type'] = normalized_result.get('result_type')
        session['last_result_summary'] = natural_message
        session['last_tool_run_at'] = datetime.now().isoformat()
        persist_session_func(session_id, session)
        print(f"[Memory] Stored result type={normalized_result.get('result_type')} for follow-up questions")
        
        # ===== EXECUTION BLUEPRINT MEMORY =====
        # Store navigation intelligence for stable repeat runs
        data = parsed_result.get("data")
        
        result_signature = {
            "type": type(data).__name__,
            "top_keys": list(data.keys())[:5] if isinstance(data, dict) else None,
            "list_length": len(data) if isinstance(data, list) else None
        }
        
        success_keyword = None
        if isinstance(data, dict):
            for k in data.keys():
                if "schedule" in k.lower():
                    success_keyword = "schedule"
                    break
                if "order" in k.lower():
                    success_keyword = "order"
                    break
                if "shift" in k.lower():
                    success_keyword = "shift"
                    break
                if "appointment" in k.lower():
                    success_keyword = "appointment"
                    break
        
        # Store blueprint in node metadata
        node_id = matched_node_id if matched_node_id else (list(saved_nodes.keys())[0] if saved_nodes else None)
        if node_id and node_id in saved_nodes:
            node_data = saved_nodes[node_id]
            if "metadata" not in node_data:
                node_data["metadata"] = {}
            
            node_data["metadata"]["execution_blueprint"] = {
                "task_goal_template": session.get("original_task"),
                "success_keyword": success_keyword,
                "result_signature": result_signature,
                "portal_url": node_data.get("portal_url")
            }
            
            persist_node_func(node_id, node_data)
            print(f"[Blueprint] Stored execution blueprint for node {node_id[:8]} (keyword={success_keyword})")
        
        # Include streaming_url if available
        streaming_url = session.get('streaming_url') or parsed_result.get('streaming_url')
        
        response = {
            "type": "text",
            "message": message,
            "session_id": session_id,
            "data": normalized_result  # Include normalized data for debugging/future UI
        }
        
        if streaming_url:
            response['streaming_url'] = streaming_url
            print(f"[Result Handler] Including streaming_url in response: {streaming_url}")
        
        return response
    
    # CASE 2: Needs input - missing credential or extra field
    elif status == 'needs_input':
        print(f"[Result Handler] TinyFish needs additional input")
        
        # Update session to waiting for extra input
        update_session_mode(session, 'waiting_extra_input', 'waiting_input')
        
        # Extract what's needed
        field = parsed_result.get('field')
        field_message = parsed_result.get('message', '')
        
        if field:
            # Add to required fields if not already there
            required_fields = session.get('required_fields', [])
            field_names = [f.get('field') for f in required_fields]
            
            if field not in field_names:
                required_fields.append({
                    "field": field,
                    "label": field.replace('_', ' ').title(),
                    "type": "password" if any(s in field.lower() for s in ['password', 'pin', 'otp', 'secret']) else "text"
                })
                set_required_fields(session, required_fields)
            
            message = f"I need your {field.replace('_', ' ')} to proceed."
            if field_message:
                message += f" {field_message}"
            message += " You can provide it naturally in your next message."
        else:
            message = "I need some additional information to proceed. "
            if field_message:
                message += field_message
            else:
                message += "Could you provide the missing details?"
        
        persist_session_func(session_id, session)
        
        # Include streaming_url if available
        streaming_url = session.get('streaming_url') or parsed_result.get('streaming_url')
        
        response = {
            "type": "text",
            "message": message,
            "session_id": session_id,
            "data": None
        }
        
        if streaming_url:
            response['streaming_url'] = streaming_url
            print(f"[Result Handler] Including streaming_url in response: {streaming_url}")
        
        return response
    
    # CASE 3: Next step - treat as progress (don't reintroduce multi-step orchestration)
    elif status == 'next_step':
        print(f"[Result Handler] TinyFish returned next_step - treating as progress")
        
        # Extract any partial data
        data = parsed_result.get('data', {})
        next_action = parsed_result.get('next_action', '')
        
        # For now, treat next_step as incomplete but progressing
        # Could retry or ask user for clarification
        message = "I'm making progress on your task. "
        if next_action:
            message += f"Next: {next_action}"
        
        # Keep session in running state
        update_session_mode(session, 'running', 'running')
        persist_session_func(session_id, session)
        
        # Include streaming_url if available
        streaming_url = session.get('streaming_url') or parsed_result.get('streaming_url')
        
        response = {
            "type": "text",
            "message": message,
            "session_id": session_id,
            "data": data
        }
        
        if streaming_url:
            response['streaming_url'] = streaming_url
        
        return response
    
    # CASE 4: Error
    elif status == 'error':
        error_message = parsed_result.get('message', 'An unknown error occurred')
        print(f"[Result Handler] TinyFish error: {error_message}")
        
        # Increment retry count
        increment_retry(session)
        retry_count = session.get('retry_count', 0)
        max_retries = 2
        
        if retry_count < max_retries:
            # Allow retry
            update_session_mode(session, 'ready', 'waiting_input')
            message = f"I encountered an issue: {error_message}\n\nWould you like me to try again? You can also provide additional details if needed."
        else:
            # Max retries reached
            update_session_mode(session, 'error', 'error')
            message = f"I'm sorry, I couldn't complete this task after {retry_count} attempts. Error: {error_message}"
        
        persist_session_func(session_id, session)
        
        # Include streaming_url if available
        streaming_url = session.get('streaming_url') or parsed_result.get('streaming_url')
        
        response = {
            "type": "text",
            "message": message,
            "session_id": session_id,
            "data": None
        }
        
        if streaming_url:
            response['streaming_url'] = streaming_url
        
        return response
    
    # CASE 5: Unknown status
    else:
        print(f"[Result Handler] Unknown status: {status}")
        
        update_session_mode(session, 'error', 'error')
        persist_session_func(session_id, session)
        
        return {
            "type": "text",
            "message": f"I received an unexpected response from the portal. Status: {status}",
            "session_id": session_id,
            "data": None
        }
