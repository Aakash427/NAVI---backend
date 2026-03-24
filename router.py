"""
Message Router for Navi
Determines routing for incoming user messages
"""

from utils import normalize_portal_key, portals_match
from result_reasoning import has_stored_result, should_allow_rerun, is_execution_too_recent, looks_like_followup_question


def route_message(user_message, sessions, saved_nodes, edges_storage=None):
    """
    Route incoming user message to appropriate handler.
    
    Args:
        user_message: User's message
        sessions: Active sessions dict
        saved_nodes: All saved portal nodes
        edges_storage: Edge connections dict (for connection gating)

    Returns:
        {
            "route": "active_session_input" | "followup_reasoning" | "new_task" | "repeat_run" | "general_chat",
            "session_id": str | None,
            "matched_node_id": str | None
        }
    """
    
    # Filter saved_nodes to only include nodes connected to navi_agent
    if edges_storage:
        connected_node_ids = set()
        for edge_id, edge_data in edges_storage.items():
            if edge_data.get('source') == 'navi_agent':
                connected_node_ids.add(edge_data.get('target'))
        
        # Filter saved_nodes to only connected nodes
        saved_nodes = {node_id: node_data for node_id, node_data in saved_nodes.items() 
                      if node_id in connected_node_ids}
        
        print(f"[Router] Connection gating: {len(connected_node_ids)} connected nodes out of {len(saved_nodes)} total")
    else:
        print(f"[Router] No edge storage - using all {len(saved_nodes)} nodes")

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
    
    print(f"[Router] Checking {len(saved_nodes)} saved nodes for match with message: '{user_message[:50]}...'")
    
    for node_id, node_data in saved_nodes.items():
        portal_name = node_data.get('portal_name', '').lower()
        portal_url = node_data.get('portal_url', '').lower()
        portal_key = node_data.get('portal_key', '').lower()
        
        print(f"[Router] Checking node {node_id[:8]}: name={portal_name}, key={portal_key}")
        
        # Check if message mentions this portal
        if portal_name and portal_name in message_lower:
            matched_node_id = node_id
            print(f"[Router] ✓ Matched saved node {node_id[:8]} by portal_name: {portal_name}")
            break
        
        if portal_url:
            # Extract domain from URL
            domain = portal_url.replace('https://', '').replace('http://', '').split('/')[0]
            if domain in message_lower:
                matched_node_id = node_id
                print(f"[Router] ✓ Matched saved node {node_id[:8]} by domain: {domain}")
                break
        
        if portal_key and portal_key in message_lower.replace(' ', '_').replace('-', '_').replace('.', '_'):
            matched_node_id = node_id
            print(f"[Router] ✓ Matched saved node {node_id[:8]} by portal_key: {portal_key}")
            break
    
    if not matched_node_id:
        print(f"[Router] No portal matched for message")
    
    # ===== RULE: If we have stored result → prefer reasoning =====
    if matched_node_id:
        # Check if portal has cached result in metadata
        portal_node = saved_nodes.get(matched_node_id)
        has_portal_cache = False
        if portal_node:
            metadata = portal_node.get('metadata', {})
            has_portal_cache = 'last_result' in metadata and metadata.get('last_result')
            print(f"[Router] Portal {matched_node_id[:8]} metadata keys: {list(metadata.keys())}")
            print(f"[Router] Portal has cached result: {has_portal_cache}")
            if has_portal_cache:
                print(f"[Router] Cache updated at: {metadata.get('last_result_updated_at', 'unknown')}")
        
        # Find session associated with this node
        node_session = None
        session_id = None
        for sid, sess in sessions.items():
            if sess.get("matched_node_id") == matched_node_id:
                node_session = sess
                session_id = sid
                break
        
        has_session_result = node_session and has_stored_result(node_session)
        print(f"[Router] Session has result: {has_session_result}, Portal has cache: {has_portal_cache}")
        
        # Check if we have cached result (either in session or portal metadata)
        has_cached_result = has_session_result or has_portal_cache
        
        if has_cached_result:
            # Detect explicit refresh requests
            refresh_keywords = ['refresh', 'update', 'fetch latest', 'run again', 'check again', 
                              'get new', 'reload', 'rerun', 'latest', 'current', 'now']
            is_explicit_refresh = any(keyword in message_lower for keyword in refresh_keywords)
            
            if is_explicit_refresh:
                print(f"[Router] Explicit refresh requested -> repeat_run")
                return {
                    "route": "repeat_run",
                    "session_id": session_id,
                    "matched_node_id": matched_node_id
                }
            
            # Natural follow-up questions should use cached result
            # Examples: "When is my next game?", "What is the Ducks game date?", "How many entries?"
            print(f"[Router] Using cached portal result for follow-up question")
            if has_portal_cache:
                print(f"[Router] Portal cache available from {metadata.get('last_result_updated_at', 'unknown')}")
            
            # Create or reuse session for reasoning
            if not session_id:
                # No existing session - we'll need to create one in the handler
                # But we can still route to followup_reasoning with the matched node
                print(f"[Router] No active session, will use portal cache for reasoning")
            
            return {
                "route": "followup_reasoning",
                "session_id": session_id,
                "matched_node_id": matched_node_id
            }
        
        # No cached result - allow repeat run
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
