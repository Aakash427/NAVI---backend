"""
Message Router for Navi
Determines routing for incoming user messages
"""

from utils import normalize_portal_key, portals_match
from result_reasoning import has_stored_result, should_allow_rerun, is_execution_too_recent, looks_like_followup_question


def route_message(user_message, sessions, saved_nodes):
    """
    Route incoming user message to appropriate handler.

    Returns:
        {
            "route": "active_session_input" | "followup_reasoning" | "new_task" | "repeat_run" | "general_chat",
            "session_id": str | None,
            "matched_node_id": str | None
        }
    """

    # RULE 1: Check for active session waiting for input
    # Priority: most recent session in waiting state
    active_session_id = None
    active_session = None

    for session_id, session_data in sorted(sessions.items(), 
                                          key=lambda x: x[1].get('updated_at', ''), 
                                          reverse=True):
        status = session_data.get('status')
        mode = session_data.get('mode')
        
        if status == 'waiting_input' or mode == 'waiting_extra_input' or mode == 'collecting':
            active_session_id = session_id
            active_session = session_data
            print(f"[Router] Found active session {session_id[:8]} in mode={mode}, status={status}")
            break
    
    if active_session_id:
        return {
            "route": "active_session_input",
            "session_id": active_session_id,
            "matched_node_id": active_session.get('matched_node_id')
        }
    
    # RULE 2: Quick intent check - does this look like a task request?
    # Simple heuristics to avoid full Gemini call for general chat
    task_indicators = [
        'fetch', 'get', 'check', 'retrieve', 'show', 'find', 'search',
        'schedule', 'report', 'data', 'login', 'access', 'open',
        'my', 'from', 'portal', 'website', '.com', 'http'
    ]
    
    message_lower = user_message.lower()
    looks_like_task = any(indicator in message_lower for indicator in task_indicators)
    
    if not looks_like_task:
        # Check if this could be a follow-up question about stored results
        # Find most recent session with stored result
        recent_session_with_result = None
        for session_id, session_data in sorted(sessions.items(), 
                                              key=lambda x: x[1].get('updated_at', ''), 
                                              reverse=True):
            if has_stored_result(session_data):
                recent_session_with_result = (session_id, session_data)
                break
        
        if recent_session_with_result:
            session_id, session_data = recent_session_with_result
            print(f"[Router] Routing as followup_reasoning for session {session_id[:8]}")
            return {
                "route": "followup_reasoning",
                "session_id": session_id,
                "matched_node_id": session_data.get('matched_node_id')
            }
        
        # Likely general conversation
        print(f"[Router] Message doesn't look like a task request")
        return {
            "route": "general_chat",
            "session_id": None,
            "matched_node_id": None
        }
    
    # RULE 3: Check if this matches a saved portal (repeat run)
    # Look for portal name or URL mentions
    matched_node_id = None
    
    for node_id, node_data in saved_nodes.items():
        portal_name = node_data.get('portal_name', '').lower()
        portal_url = node_data.get('portal_url', '').lower()
        portal_key = node_data.get('portal_key', '').lower()
        
        # Check if message mentions this portal
        if portal_name and portal_name in message_lower:
            matched_node_id = node_id
            print(f"[Router] Matched saved node {node_id[:8]} by portal_name: {portal_name}")
            break
        
        if portal_url:
            # Extract domain from URL
            domain = portal_url.replace('https://', '').replace('http://', '').split('/')[0]
            if domain in message_lower:
                matched_node_id = node_id
                print(f"[Router] Matched saved node {node_id[:8]} by domain: {domain}")
                break
        
        if portal_key and portal_key in message_lower.replace(' ', '_').replace('-', '_').replace('.', '_'):
            matched_node_id = node_id
            print(f"[Router] Matched saved node {node_id[:8]} by portal_key: {portal_key}")
            break
    
    # ===== RULE: If we have stored result → prefer reasoning =====
    if matched_node_id:
        # Find session associated with this node
        node_session = None
        session_id = None
        for sid, sess in sessions.items():
            if sess.get("matched_node_id") == matched_node_id:
                node_session = sess
                session_id = sid
                break
        
        if node_session and has_stored_result(node_session):
            # 1️⃣ Semantic follow-up ALWAYS wins
            if looks_like_followup_question(user_message):
                print("[Router] Semantic follow-up detected → routing to reasoning")
                return {
                    "route": "followup_reasoning",
                    "session_id": session_id,
                    "matched_node_id": matched_node_id
                }
            
            # 2️⃣ Explicit rerun allowed
            if should_allow_rerun(user_message, node_session):
                print("[Router] Explicit rerun requested → repeat_run allowed")
                return {
                    "route": "repeat_run",
                    "session_id": session_id,
                    "matched_node_id": matched_node_id
                }
            
            # 3️⃣ Execution cooldown guard
            if is_execution_too_recent(node_session):
                print("[Router] Cooldown guard → reasoning instead of rerun")
                return {
                    "route": "followup_reasoning",
                    "session_id": session_id,
                    "matched_node_id": matched_node_id
                }
            
            # 4️⃣ Default fallback → reasoning
            print("[Router] Stored result exists → default reasoning")
            return {
                "route": "followup_reasoning",
                "session_id": session_id,
                "matched_node_id": matched_node_id
            }
        
        # No stored result - allow repeat run
        return {
            "route": "repeat_run",
            "session_id": None,
            "matched_node_id": matched_node_id
        }
    
    # RULE 4: New task
    print(f"[Router] Routing as new_task")
    return {
        "route": "new_task",
        "session_id": None,
        "matched_node_id": None
    }
