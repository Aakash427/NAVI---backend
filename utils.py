"""
Utility functions for Navi backend
Handles field normalization, logging, and session helpers
"""

import json
import hashlib
from datetime import datetime
from urllib.parse import urlparse
import re

# Sensitive field keywords that should be masked in logs
SENSITIVE_KEYWORDS = ['password', 'pin', 'otp', 'secret', 'token', 'api_key', 'apikey']

def normalize_portal_key(portal_name, portal_url=None):
    """
    Generate a stable canonical portal key for identity matching.
    Prioritizes URL domain over freeform names.
    """
    # If URL exists, extract domain-based key
    if portal_url:
        try:
            parsed = urlparse(portal_url)
            domain = parsed.netloc.lower()
            
            # Remove www prefix
            domain = re.sub(r'^www\.', '', domain)
            
            # Convert to underscore-separated key
            # e.g., ess.abimm.com -> ess_abimm_com
            portal_key = domain.replace('.', '_').replace('-', '_')
            
            return portal_key
        except:
            pass
    
    # Fallback to name-based key if URL not available or parsing failed
    if portal_name:
        # Lowercase, trim, replace spaces/hyphens with underscores
        key = portal_name.lower().strip()
        key = re.sub(r'[\s-]+', '_', key)
        # Remove special characters except underscores
        key = re.sub(r'[^a-z0-9_]', '', key)
        return key
    
    return "unknown_portal"

def get_portal_aliases(portal_name, portal_url=None):
    """
    Provide normalized candidate identifiers for fuzzy matching.
    Returns list of possible identity strings.
    """
    aliases = []
    
    # Primary key
    primary_key = normalize_portal_key(portal_name, portal_url)
    aliases.append(primary_key)
    
    # Name-based key (if different from primary)
    if portal_name:
        name_key = normalize_portal_key(portal_name, None)
        if name_key != primary_key:
            aliases.append(name_key)
    
    # Domain without TLD (if URL exists)
    if portal_url:
        try:
            parsed = urlparse(portal_url)
            domain = parsed.netloc.lower()
            domain = re.sub(r'^www\.', '', domain)
            
            # Get hostname without TLD: ess.abimm.com -> ess_abimm
            parts = domain.split('.')
            if len(parts) > 1:
                hostname_no_tld = '_'.join(parts[:-1])
                if hostname_no_tld not in aliases:
                    aliases.append(hostname_no_tld)
        except:
            pass
    
    return aliases

def portals_match(node_data, portal_name, portal_url):
    """
    Deterministic portal matching using portal_key and normalized identifiers.
    Returns True if saved node matches the current request.
    """
    # Compute incoming portal key
    incoming_key = normalize_portal_key(portal_name, portal_url)
    incoming_aliases = get_portal_aliases(portal_name, portal_url)
    
    # Get node's portal key
    node_key = node_data.get('portal_key')
    node_url = node_data.get('portal_url')
    node_name = node_data.get('portal_name')
    
    # If node doesn't have portal_key yet (old data), compute it
    if not node_key:
        node_key = normalize_portal_key(node_name, node_url)
    
    # Priority 1: Exact portal_key match
    if node_key == incoming_key:
        return True
    
    # Priority 2: URL domain match (if both have URLs)
    if portal_url and node_url:
        try:
            incoming_domain = urlparse(portal_url).netloc.lower().replace('www.', '')
            node_domain = urlparse(node_url).netloc.lower().replace('www.', '')
            if incoming_domain == node_domain:
                return True
        except:
            pass
    
    # Priority 3: Alias overlap
    node_aliases = get_portal_aliases(node_name, node_url)
    for alias in incoming_aliases:
        if alias in node_aliases:
            return True
    
    return False

def merge_credentials(saved_creds, provided_creds, cipher):
    """
    Safely merge saved credentials with newly provided credentials.
    
    Args:
        saved_creds: dict of already-encrypted credentials from saved node
        provided_creds: dict of plaintext credentials from current message
        cipher: Fernet cipher instance for encryption
    
    Returns:
        dict of merged credentials (sensitive fields encrypted)
    """
    merged = {}
    
    # Start with saved credentials (already encrypted)
    if saved_creds:
        merged.update(saved_creds)
    
    # Add/override with provided credentials (encrypt sensitive ones)
    if provided_creds:
        for key, value in provided_creds.items():
            # Check if this is a sensitive field
            is_sensitive = any(s in key.lower() for s in ['password', 'pin', 'secret', 'otp', 'token', 'api_key', 'apikey'])
            
            if is_sensitive:
                # Encrypt before storing
                try:
                    # Only encrypt if not already encrypted
                    if isinstance(value, str) and not value.startswith('gAAAAA'):
                        merged[key] = cipher.encrypt(str(value).encode()).decode()
                    else:
                        merged[key] = value
                except Exception as e:
                    print(f"[Credential Merge] Error encrypting {key}: {e}")
                    merged[key] = value
            else:
                # Store plaintext for non-sensitive fields
                merged[key] = value
    
    return merged

def normalize_fields(raw_fields):
    """
    Normalize field structures to consistent frontend contract
    
    Accepts variations like:
    - {"name": "...", "label": "...", "type": "..."}
    - {"field": "...", "label": "...", "type": "..."}
    - Missing label/type
    
    Always returns:
    - {"field": "exact_name", "label": "Human Readable", "type": "text|password|..."}
    """
    if not raw_fields:
        return []
    
    normalized = []
    for f in raw_fields:
        if not isinstance(f, dict):
            continue
        
        # Extract field name from either 'name' or 'field' key
        field_name = f.get("name") or f.get("field", "")
        if not field_name:
            continue
        
        # Extract label, default to field name if missing
        label = f.get("label", field_name.replace("_", " ").title())
        
        # Extract type, default to text
        field_type = f.get("type", "text")
        
        normalized.append({
            "field": field_name,
            "label": label,
            "type": field_type
        })
    
    return normalized

def mask_credentials(creds):
    """
    Mask sensitive credential values for logging
    Returns a copy with sensitive values replaced with [MASKED]
    """
    if not isinstance(creds, dict):
        return creds
    
    masked = {}
    for key, value in creds.items():
        if any(sensitive in key.lower() for sensitive in SENSITIVE_KEYWORDS):
            masked[key] = "[MASKED]"
        else:
            masked[key] = value
    
    return masked

def log_session_event(session_id, message, extra=None):
    """
    Structured logging for session events
    Automatically masks sensitive data
    """
    timestamp = datetime.now().isoformat()
    session_short = session_id[:8] if session_id else "unknown"
    
    log_msg = f"[Session {session_short}] {message}"
    
    if extra:
        if isinstance(extra, dict):
            extra_masked = mask_credentials(extra)
            log_msg += f" | {json.dumps(extra_masked)}"
        else:
            log_msg += f" | {extra}"
    
    print(log_msg)

def compute_input_hash(input_data):
    """
    Compute hash of input data for deduplication
    """
    if not input_data:
        return ""
    
    # Sort keys for consistent hashing
    input_str = json.dumps(input_data, sort_keys=True)
    return hashlib.md5(input_str.encode()).hexdigest()

def determine_credential_readiness(session, parsed_intent=None, discovered_fields=None):
    """Determine if session has enough credentials to attempt TinyFish execution"""
    credentials = session.get("credentials", {})
    
    # If we have saved node credentials, we're ready
    if credentials and len(credentials) >= 1:
        # Check if we have at least one login-like credential
        has_identifier = any(k.lower() in ['username', 'user', 'userid', 'login', 'loginid', 'email', 'account'] for k in credentials.keys())
        has_password = any(k.lower() in ['password', 'pass', 'pwd', 'pin'] for k in credentials.keys())
        
        # If we have both identifier and password, definitely ready
        if has_identifier and has_password:
            return True, "ready_to_run"
        
        # If we have at least 2 credentials, assume ready (pragmatic for MVP)
        if len(credentials) >= 2:
            return True, "ready_to_run"
    
    # If discovered fields exist and we don't have them, not ready
    if discovered_fields:
        missing = [f for f in discovered_fields if f.get("field") not in credentials and f.get("name") not in credentials]
        if missing:
            return False, "collecting_credentials"
    
    # If no credentials at all, not ready
    if not credentials:
        return False, "collecting_credentials"
    
    # Default: if we have some credentials, try execution
    return True, "ready_to_run"

def build_execution_goal_comprehensive(session):
    """
    Build comprehensive TinyFish execution goal for single-run execution.
    
    This goal tells TinyFish to:
    - Start fresh from portal URL
    - Use ALL known credentials
    - Complete entire flow in one run
    - Handle multi-page login
    - Continue through authentication
    - Complete user's task
    - Return structured results
    """
    portal_url = session.get('portal_url')
    original_task = session.get('original_task')
    credentials = session.get('credentials', {})
    
    # Decrypt credentials for TinyFish
    from cryptography.fernet import Fernet
    import os
    ENCRYPTION_KEY = os.getenv('ENCRYPTION_KEY', Fernet.generate_key())
    cipher = Fernet(ENCRYPTION_KEY)
    
    decrypted_creds = {}
    for key, value in credentials.items():
        if value and isinstance(value, str) and value.startswith('gAAAAA'):
            try:
                decrypted_creds[key] = cipher.decrypt(value.encode()).decode()
            except:
                decrypted_creds[key] = value
        else:
            decrypted_creds[key] = value
    
    # Build credential description
    cred_desc = "\n".join([f"- {k}: {v}" for k, v in decrypted_creds.items()])
    
    goal = f"""Complete this task in ONE FULL RUN from start to finish:

Task: {original_task}

Starting URL: {portal_url}

Available credentials:
{cred_desc}

Instructions:
1. Navigate to the portal URL
2. If you encounter a login page:
   - Use the provided credentials to log in
   - Handle multi-page login flows if present
   - Continue through all authentication steps
   - If a credential field is not visible, try common variations or skip it
3. Once authenticated, complete the user's task:
   - Navigate to the appropriate section
   - Extract the requested data
   - Return structured results
4. If you encounter a missing credential that blocks progress:
   - Return structured JSON: {{"status": "needs_input", "field": "field_name", "message": "description"}}
5. If successful:
   - Return structured JSON: {{"status": "complete", "data": {{...}}}}
6. If an error occurs:
   - Return structured JSON: {{"status": "error", "message": "description"}}

CRITICAL REQUIREMENTS:
- You MUST finish the task and return COMPLETE status
- If login fails or page blocks you, return status "error" immediately
- Do NOT keep navigating endlessly
- Maximum navigation steps: 15
- If you cannot find the requested data after login, return error
- Do NOT assume persistent browser sessions
- Complete the ENTIRE flow in this single run
- Use ALL provided credentials as needed
- Handle login and task completion in one execution
- Return structured JSON results
"""
    
    return goal

def build_execution_goal(session):
    """Build single comprehensive TinyFish goal for stateless execution"""
    portal_url = session.get("portal_url")
    task = session.get("original_task")
    credentials = session.get("credentials", {})
    mode = session.get("mode", "ready_to_run")
    
    # Build credential display (mask sensitive values)
    cred_display = {}
    for field_name in credentials.keys():
        if any(s in field_name.lower() for s in SENSITIVE_KEYWORDS):
            cred_display[field_name] = "[PROVIDED]"
        else:
            value = credentials[field_name]
            if isinstance(value, str) and value.startswith('gAAAAA'):
                cred_display[field_name] = "[PROVIDED]"
            else:
                cred_display[field_name] = value
    
    # Single comprehensive goal for full execution
    return f"""Complete this task in ONE FULL RUN. TinyFish is stateless - you must do everything from start to finish.

Portal URL: {portal_url}

User's Task: {task}

Available Credentials:
{json.dumps(cred_display, indent=2)}

INSTRUCTIONS:
1. Navigate to the portal URL
2. This may be a MULTI-PAGE login flow. Complete ALL login pages:
   - Look for login fields on the current page
   - Fill them with matching credentials from above
   - Click Next/Continue/Login/Submit
   - If you see MORE login fields on the next page, fill those too
   - Keep going until you reach the authenticated dashboard/home page
3. Once logged in, complete the user's task:
   - Navigate to the appropriate section
   - Extract or perform the requested action
   - Gather all necessary data
4. Return results

RESPONSE FORMAT:
- If you need a credential you DON'T have:
  {{
    "status": "needs_input",
    "field_needed": "exact_field_name",
    "label": "Human Label",
    "type": "text|password|email",
    "message": "Please provide your X to continue"
  }}

- If you successfully complete the task:
  {{
    "status": "complete",
    "data": {{extracted_data_here}},
    "message": "Task completed successfully"
  }}

- If you encounter an error:
  {{
    "status": "error",
    "reason": "description of what went wrong"
  }}

CRITICAL:
- This is a STATELESS execution. You cannot continue from where you left off.
- Use ALL provided credentials intelligently across multiple login pages.
- Do NOT ask for credentials that are already provided unless they clearly don't work.
- Complete the ENTIRE flow (login + task) in this single run.
- If the login has multiple pages, keep filling and submitting until you reach the main authenticated area."""

def build_discovery_goal(portal_url):
    """Optional lightweight discovery for when no credentials are provided"""
    return f"""Visit {portal_url} and analyze the login page.

Identify ALL visible input fields that appear to be for user login/authentication.

For each field, return:
- field name or id attribute
- label text (visible to user)
- input type (text, password, email, number, checkbox, etc.)

Return JSON:
{{
  "status": "complete",
  "data": {{
    "fields": [
      {{"name": "field_name", "label": "Field Label", "type": "text"}},
      {{"name": "password", "label": "Password", "type": "password"}}
    ]
  }}
}}

Do NOT fill anything. Only identify and report the fields."""

def handle_tinyfish_execution_result(session, parsed_result):
    """
    Process TinyFish execution result and update session state accordingly
    Returns: (mode, status, should_save_node, frontend_response_data)
    """
    status = parsed_result.get("status")
    
    if status == "complete":
        # Success - task completed
        return "complete", "complete", True, {
            "type": "text",
            "message": parsed_result.get("message", "Task completed successfully!"),
            "data": parsed_result.get("data", {})
        }
    
    elif status == "needs_input":
        # Missing credential - ask user
        field_needed = parsed_result.get("field_needed", "")
        
        # Store requested field
        session["last_fields_requested"] = [field_needed]
        
        return "waiting_extra_input", "waiting_input", False, {
            "type": "input_request",
            "field": field_needed,
            "label": parsed_result.get("label", field_needed),
            "field_type": parsed_result.get("type", "text"),
            "message": parsed_result.get("message", f"Please provide your {parsed_result.get('label', field_needed)}")
        }
    
    elif status == "next_step":
        # Progress indicator - check if we need more fields
        fields_needed = parsed_result.get("fields_found", []) or parsed_result.get("fields", [])
        
        if fields_needed:
            # Normalize and check for missing fields
            normalized = normalize_fields(fields_needed)
            credentials = session.get("credentials", {})
            
            missing = [f for f in normalized if f.get("field") not in credentials]
            
            if missing:
                session["last_fields_requested"] = [f.get("field") for f in missing]
                
                return "waiting_extra_input", "waiting_input", False, {
                    "type": "multi_input_request",
                    "fields": missing,
                    "message": "Please provide the following credentials:"
                }
        
        # No missing fields - treat as success
        return "complete", "complete", True, {
            "type": "text",
            "message": parsed_result.get("message", "Task progressing..."),
            "data": parsed_result.get("data", {})
        }
    
    elif status == "error":
        # Error occurred
        return "error", "error", False, {
            "type": "text",
            "message": f"I encountered an issue: {parsed_result.get('reason', 'Unknown error')}"
        }
    
    else:
        # Unknown status
        return "error", "error", False, {
            "type": "text",
            "message": "Unexpected response from automation. Please try again."
        }

def old_build_goal_for_phase_DEPRECATED(session):
    """DEPRECATED - kept for reference only"""
    phase = session.get("phase", "discover")
    portal_url = session.get("portal_url")
    task = session.get("original_task")
    credentials = session.get("credentials", {})
    
    if phase == "discover":
        return f"""Visit {portal_url} and analyze the page.

Identify ALL visible input fields on the current page.

For each field, return:
- field name or id attribute
- label text (visible to user)
- placeholder text (if any)
- input type (text, password, email, number, checkbox, etc.)

Return a JSON response:
{{
  "status": "complete",
  "data": {{
    "fields": [
      {{"name": "field_name", "label": "Field Label", "type": "text", "placeholder": "hint text"}},
      {{"name": "password", "label": "Password", "type": "password", "placeholder": ""}}
    ]
  }}
}}

Do NOT fill anything yet. Only identify and report the fields."""
    
    elif phase == "login":
        # Build credential display (mask sensitive values, don't decrypt here)
        # Decryption happens in run_orchestration_loop before calling TinyFish
        cred_display = {}
        for field_name in credentials.keys():
            if any(s in field_name.lower() for s in SENSITIVE_KEYWORDS):
                cred_display[field_name] = "[PROVIDED]"
            else:
                # For encrypted values, show placeholder
                value = credentials[field_name]
                if isinstance(value, str) and value.startswith('gAAAAA'):
                    cred_display[field_name] = "[PROVIDED]"
                else:
                    cred_display[field_name] = value
        
        return f"""Complete the multi-page login flow for this portal.

Available credentials:
{json.dumps(cred_display, indent=2)}

Instructions:
1. Start at {portal_url}
2. Look at the current page and identify any login fields
3. Fill any visible fields using the matching credentials from above
4. Submit the form (click Next/Continue/Login/Submit button)
5. Wait for the next page to load
6. If you see MORE login fields on the new page:
   - Fill them with matching credentials
   - Submit again
   - Repeat until you reach the main dashboard/logged-in page
7. If you need a credential you DON'T have:
   - Return: {{"status": "needs_input", "field_needed": "exact_field_name", "label": "Human Label", "type": "text|password", "message": "Please provide your X to continue"}}
8. Once you successfully reach the dashboard/main page (no more login prompts):
   - Return: {{"status": "next_step", "page_description": "logged in successfully, on dashboard"}}

IMPORTANT: 
- This is a MULTI-PAGE login. You may need to fill fields and click Next/Continue multiple times.
- Use ALL provided credentials intelligently across multiple pages.
- If the login page restarts or loops, retry with the known credentials.
- Do NOT ask for fields that are already in the provided credentials unless they clearly don't work.
- Keep going until you reach the authenticated main page or hit a missing credential."""
    
    elif phase == "task":
        # Build credential display (mask sensitive values, don't decrypt here)
        # Decryption happens in run_orchestration_loop before calling TinyFish
        cred_display = {}
        for field_name in credentials.keys():
            if any(s in field_name.lower() for s in SENSITIVE_KEYWORDS):
                cred_display[field_name] = "[PROVIDED]"
            else:
                # For encrypted values, show placeholder
                value = credentials[field_name]
                if isinstance(value, str) and value.startswith('gAAAAA'):
                    cred_display[field_name] = "[PROVIDED]"
                else:
                    cred_display[field_name] = value
        
        return f"""Complete the user's request. You have credentials available but TinyFish is stateless, so you must log in again.

Task: {task}

Available credentials:
{json.dumps(cred_display, indent=2)}

Instructions:
1. Navigate to {portal_url}
2. Complete the login process using the provided credentials
3. Once logged in, navigate to the appropriate section for the task
4. Extract or perform the requested action
5. Return the result

If you encounter additional input fields (like OTP, verification code):
{{
  "status": "needs_input",
  "field_needed": "field_name",
  "label": "Human Label",
  "type": "text",
  "message": "Please provide X to continue"
}}

When task is complete:
{{
  "status": "complete",
  "data": {{
    "result": "extracted data or confirmation"
  }},
  "message": "Task completed successfully"
}}"""
    
    else:
        # Fallback
        return f"Navigate to {portal_url} and complete the requested task: {task}"
