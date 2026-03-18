"""
Session State Manager for Navi
Manages session lifecycle, credentials, and readiness evaluation
"""

from datetime import datetime
from cryptography.fernet import Fernet


def create_session(session_id, portal_key, portal_name, portal_url, original_task, node_type="browser", matched_node_id=None):
    """
    Create a new session with clean schema.
    
    Returns session dict following new schema.
    """
    return {
        "session_id": session_id,
        "portal_key": portal_key,
        "portal_name": portal_name,
        "portal_url": portal_url,
        "original_task": original_task,
        "node_type": node_type,
        "mode": "collecting",  # collecting | ready | running | waiting_extra_input | complete | error
        "status": "waiting_input",  # running | waiting_input | complete | error
        "credentials": {},
        "required_fields": [],  # normalized field objects
        "missing_fields": [],  # subset of required_fields
        "history": [],
        "retry_count": 0,
        "busy": False,
        "streaming_url": None,
        "execution_started_at": None,
        "live_status": "idle",  # idle | starting | running | complete | error | timeout
        "live_message": None,
        "last_result": None,
        "last_result_type": None,
        "last_result_summary": None,
        "last_tool_run_at": None,
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
        "matched_node_id": matched_node_id
    }


def update_session_credentials(session, new_credentials, cipher):
    """
    Merge new credentials into session, encrypting sensitive fields.
    
    Args:
        session: Session dict
        new_credentials: Dict of new credentials to merge
        cipher: Fernet cipher for encryption
    
    Returns:
        Updated session dict
    """
    existing_creds = session.get('credentials', {}) or {}
    
    # Merge credentials
    for key, value in new_credentials.items():
        if not value:
            continue
        
        # Check if this is a sensitive field
        is_sensitive = any(s in key.lower() for s in ['password', 'pin', 'secret', 'otp', 'token', 'api_key', 'apikey'])
        
        if is_sensitive:
            # Encrypt before storing (only if not already encrypted)
            try:
                if isinstance(value, str) and not value.startswith('gAAAAA'):
                    existing_creds[key] = cipher.encrypt(str(value).encode()).decode()
                else:
                    existing_creds[key] = value
            except Exception as e:
                print(f"[Session Manager] Error encrypting {key}: {e}")
                existing_creds[key] = value
        else:
            # Store plaintext for non-sensitive fields
            existing_creds[key] = value
    
    session['credentials'] = existing_creds
    session['updated_at'] = datetime.now().isoformat()
    
    print(f"[Session Manager] Updated credentials: {list(existing_creds.keys())}")
    
    return session


def evaluate_session_readiness(session):
    """
    Evaluate if session has enough credentials to execute.
    
    Returns:
        {
            "is_ready": bool,
            "missing_fields": [field objects],
            "reason": str
        }
    """
    credentials = session.get('credentials', {})
    required_fields = session.get('required_fields', [])
    
    # Count non-empty credentials
    cred_count = len([k for k, v in credentials.items() if v])
    
    print(f"[Session Manager] Evaluating readiness: {cred_count} credentials, {len(required_fields)} required fields")
    
    # If no credentials at all
    if cred_count == 0:
        return {
            "is_ready": False,
            "missing_fields": required_fields if required_fields else [],
            "reason": "No credentials provided"
        }
    
    # If required_fields are known, check against them
    if required_fields:
        missing = []
        for field in required_fields:
            field_name = field.get('field')
            if field_name not in credentials or not credentials[field_name]:
                missing.append(field)
        
        if missing:
            print(f"[Session Manager] Missing required fields: {[f.get('field') for f in missing]}")
            return {
                "is_ready": False,
                "missing_fields": missing,
                "reason": f"Missing required fields: {', '.join([f.get('field') for f in missing])}"
            }
        else:
            print(f"[Session Manager] All required fields present")
            return {
                "is_ready": True,
                "missing_fields": [],
                "reason": "All required fields present"
            }
    
    # If required_fields unknown, use heuristic
    # Need at least 2 credentials OR one identifier + one password-like field
    has_password = any('password' in k.lower() or 'pass' in k.lower() or 'pin' in k.lower() 
                       for k in credentials.keys())
    has_identifier = any(k.lower() in ['username', 'user', 'email', 'loginid', 'userid', 'login'] 
                         for k in credentials.keys())
    
    if cred_count >= 2 or (has_identifier and has_password):
        print(f"[Session Manager] Heuristic: sufficient credentials (count={cred_count}, has_password={has_password}, has_identifier={has_identifier})")
        return {
            "is_ready": True,
            "missing_fields": [],
            "reason": "Sufficient credentials based on heuristic"
        }
    else:
        print(f"[Session Manager] Heuristic: insufficient credentials")
        return {
            "is_ready": False,
            "missing_fields": [],
            "reason": "Need more credentials (at least username and password)"
        }


def update_session_mode(session, new_mode, new_status=None):
    """
    Update session mode and optionally status.
    Logs the transition.
    """
    old_mode = session.get('mode')
    old_status = session.get('status')
    
    session['mode'] = new_mode
    if new_status:
        session['status'] = new_status
    session['updated_at'] = datetime.now().isoformat()
    
    print(f"[Session Manager] Mode transition: {old_mode} → {new_mode}, Status: {old_status} → {session.get('status')}")
    
    return session


def set_required_fields(session, fields):
    """
    Set required fields for session.
    
    Args:
        session: Session dict
        fields: List of normalized field objects
    """
    session['required_fields'] = fields
    session['updated_at'] = datetime.now().isoformat()
    
    field_names = [f.get('field') for f in fields]
    print(f"[Session Manager] Set required fields: {field_names}")
    
    return session


def increment_retry(session):
    """
    Increment retry count for session.
    """
    session['retry_count'] = session.get('retry_count', 0) + 1
    session['updated_at'] = datetime.now().isoformat()
    
    print(f"[Session Manager] Retry count: {session['retry_count']}")
    
    return session


def get_missing_field_names(session):
    """
    Get list of missing field names as strings.
    """
    missing_fields = session.get('missing_fields', [])
    return [f.get('field') for f in missing_fields if f.get('field')]
