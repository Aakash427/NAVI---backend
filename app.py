import atexit
import os
import json
import uuid
import math
import requests
import sqlite3
import hashlib
import time
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, Response, send_from_directory
from flask_cors import CORS
from dotenv import load_dotenv
from google import genai
from google.genai import types
from cryptography.fernet import Fernet
import threading

load_dotenv()

print("[Startup] Starting Navi backend...")
print("[Startup] Loading modules...")

from db_helpers import init_db, load_saved_nodes, persist_node, persist_session, reset_all_state
from utils import normalize_fields, mask_credentials, log_session_event, normalize_portal_key, get_portal_aliases, portals_match, build_execution_goal_comprehensive, build_discovery_goal
from router import route_message
from extractors import extract_task_intent, extract_session_input
from session_manager import create_session, update_session_credentials, evaluate_session_readiness, update_session_mode, set_required_fields, get_missing_field_names
from result_handler import handle_execution_result

print("[Startup] All modules loaded successfully")

# Environment Variables with graceful failure handling
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
TINYFISH_API_KEY = os.getenv('TINYFISH_API_KEY')
ENCRYPTION_KEY_ENV = os.getenv('ENCRYPTION_KEY')

# Validate required environment variables
if not GEMINI_API_KEY:
    print("[STARTUP ERROR] GEMINI_API_KEY environment variable is not set")
    print("[STARTUP ERROR] Please set GEMINI_API_KEY in your deployment environment")
    raise ValueError("GEMINI_API_KEY is required")

if not TINYFISH_API_KEY:
    print("[STARTUP ERROR] TINYFISH_API_KEY environment variable is not set")
    print("[STARTUP ERROR] Please set TINYFISH_API_KEY in your deployment environment")
    raise ValueError("TINYFISH_API_KEY is required")

if not ENCRYPTION_KEY_ENV:
    print("[STARTUP ERROR] ENCRYPTION_KEY environment variable is not set")
    print("[STARTUP ERROR] Please set ENCRYPTION_KEY in your deployment environment")
    print("[STARTUP ERROR] Generate one with: python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'")
    raise ValueError("ENCRYPTION_KEY is required")

print("[Startup] Environment variables loaded successfully")

# Configure Gemini
try:
    client = genai.Client(api_key=GEMINI_API_KEY)
    print("[Startup] Gemini client initialized")
except Exception as e:
    print(f"[STARTUP ERROR] Failed to initialize Gemini client: {e}")
    raise

# Encryption key for credentials
try:
    cipher = Fernet(ENCRYPTION_KEY_ENV.encode() if isinstance(ENCRYPTION_KEY_ENV, str) else ENCRYPTION_KEY_ENV)
    print("[Startup] Encryption cipher initialized")
except Exception as e:
    print(f"[STARTUP ERROR] Failed to initialize encryption cipher: {e}")
    print(f"[STARTUP ERROR] ENCRYPTION_KEY must be a valid Fernet key")
    raise

print("[Startup] Creating Flask app...")
app = Flask(__name__)
print("[Startup] Configuring CORS...")
CORS(app)
print("[Startup] Flask app created successfully")

# In-Memory State
saved_nodes = {}  # portal_id -> { "type": "browser" | "api", "portal_name": str, "portal_url": str, "credentials": {}, "api_key": str }
jobs = {}  # job_id -> { "intent": str, "portal_id": str, "instruction": str, "status": "waiting_auth" | "waiting_confirm" | "running" | "done" }
navi_portals = {}
edges_storage = {}  # edge_id -> { "source": str, "target": str } - stores navi_agent connections

# Orchestration Sessions (phase-based state machine)
sessions = {}  # session_id -> {
    #   "portal_name": str,
    #   "portal_url": str,
    #   "original_task": str,
    #   "credentials": {},  # accumulated credentials (encrypted)
    #   "phase": "discover" | "login" | "task" | "complete" | "error",
    #   "step": int,        # iteration counter within phase
    #   "history": [],      # list of TinyFish results so far
    #   "status": "running" | "waiting_input" | "complete" | "error",
    #   "streaming_url": str,
    #   "node_type": "browser" | "api",
    #   "last_fields_requested": [],
    #   "retry_count": int,
    #   "busy": bool,
    #   "last_input_hash": str,
    #   "last_input_time": float
    # }

# Database path
DB_PATH = os.path.join(os.path.dirname(__file__), 'navi.db')

# Initialize database and load saved nodes on startup
print("[Startup] Initializing database...")
try:
    init_db()
    print("[Startup] Database initialized")
    print("[Startup] Loading saved nodes...")
    saved_nodes = load_saved_nodes()
    print(f"[Startup] Loaded {len(saved_nodes)} saved nodes")
    
    # Log credential status on startup
    if saved_nodes:
        nodes_with_creds = 0
        for node_id, node_data in saved_nodes.items():
            creds = node_data.get('credentials', {})
            if creds and len([k for k, v in creds.items() if v]) > 0:
                nodes_with_creds += 1
        print(f"[Startup] Nodes with usable credentials: {nodes_with_creds}/{len(saved_nodes)}")
    else:
        print("[Startup] No saved nodes found")
except Exception as e:
    print(f"[STARTUP ERROR] Database initialization failed: {e}")
    print(f"[STARTUP ERROR] This is non-fatal - starting with empty state")
    saved_nodes = {}

# ===== HELPER FUNCTIONS =====

def extract_credentials_from_message(user_message, required_fields=None):
    """
    Extract credentials from natural language user message using Gemini.
    Supports patterns like:
    - Venue: Canucks
    - username - akash
    - pass=123
    - OTP 483921
    """
    
    fields_hint = ""
    if required_fields:
        fields_hint = f"\n\nThe system is specifically looking for these fields: {', '.join(required_fields)}"
    
    extraction_prompt = f"""Extract login credentials from this user message.

User message: "{user_message}"
{fields_hint}

Extract any credentials, login details, or authentication information mentioned.
Support various formats:
- "Venue: Canucks" or "Venue is Canucks"
- "username - akash" or "user: akash"
- "pass=123" or "password 123"
- "OTP 483921" or "code: 483921"

Return ONLY valid JSON in this exact format:
{{
  "credentials": {{
    "field_name": "value"
  }}
}}

If no credentials found, return:
{{
  "credentials": {{}}
}}

Examples:

Input: "Venue is Canucks, user ID is Tiwari8703, password is 750621"
Output: {{"credentials": {{"input_venue": "Canucks", "LoginId": "Tiwari8703", "password": "750621"}}}}

Input: "My OTP is 483921"
Output: {{"credentials": {{"otp": "483921"}}}}

Input: "username: john@example.com, pass: secret123"
Output: {{"credentials": {{"username": "john@example.com", "password": "secret123"}}}}
"""
    
    try:
        response = client.models.generate_content(
            model='gemini-2.0-flash-exp',
            contents=extraction_prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                max_output_tokens=500,
                temperature=0.3
            )
        )
        
        result = json.loads(response.text)
        credentials = result.get('credentials', {})
        
        print(f"[Credential Extraction] Extracted {len(credentials)} credentials: {list(mask_credentials(credentials).keys())}")
        
        return credentials
        
    except Exception as e:
        print(f"[Credential Extraction] Error: {e}")
        import traceback
        traceback.print_exc()
        return {}

def parse_intent_with_gemini(user_message, saved_nodes_dict):
    """Parse user intent using Gemini and return structured action plan"""
    
    # Create summary of saved nodes for Gemini
    saved_nodes_summary = {}
    for portal_id, node_data in saved_nodes_dict.items():
        saved_nodes_summary[portal_id] = {
            "portal_name": node_data.get("portal_name"),
            "type": node_data.get("type"),
            "portal_url": node_data.get("portal_url")
        }
    
    system_prompt = f"""You are Navi, an AI work assistant that can operate any website, portal, or API on behalf of the user.

You have access to the user's saved nodes (connected portals and APIs). 

Given the user's message, return ONLY valid JSON in this format:
{{
  "intent": string (short description of what user wants),
  "portal_name": string (name of the portal or service, e.g. "Gmail", "Shopify", "Blackboard"),
  "portal_url": string (the URL of the portal if browser-based, or "api" if it's an API service),
  "node_type": "browser" | "api" | null,
  "requires_confirmation": true | false (true for any destructive or consequential action),
  "confirmation_summary": string (plain English summary of exactly what Navi is about to do, shown to user before execution),
  "tinyfish_instruction": string (if node_type is browser: ONLY describe the user's goal. DO NOT specify field names or login steps. TinyFish will discover the login flow dynamically. Example: "Navigate to the portal, log in, and extract the user's schedule data."),
  "api_action": string (if node_type is api: describe what API call needs to be made),
  "provided_credentials": object | null (extract any credentials explicitly provided in the user's message. Look for patterns like "username is X", "password is Y", "venue ID is Z", "user ID is W", etc. Return as key-value pairs. If no credentials provided, return null),
  "message": string (friendly conversational message to show the user)
}}

Saved nodes available: {json.dumps(saved_nodes_summary, indent=2)}

If the user's request is general conversation with no action, set node_type to null and return a friendly message.

CRITICAL: For browser portals, DO NOT assume any portal structure. TinyFish will discover login fields dynamically.
Your tinyfish_instruction should ONLY describe the end goal, not the steps.

IMPORTANT: Extract credentials from the user's message if they provide them explicitly.
Examples of credential patterns:
- "Venue ID is Canucks" -> {{"input_venue": "Canucks"}} or {{"venue_id": "Canucks"}}
- "User ID is Tiwari8703" -> {{"LoginId": "Tiwari8703"}} or {{"user_id": "Tiwari8703"}}
- "Password is mypass" -> {{"password": "mypass"}}
- "My username is john@example.com" -> {{"username": "john@example.com"}}

Examples:
- "Check my work schedule" -> tinyfish_instruction: "Navigate to the portal, complete the login process, and extract all schedule data including dates, times, and locations.", provided_credentials: null
- "Fetch my ABI schedule. Venue ID is Canucks. User ID is Tiwari8703. Password is mypass." -> provided_credentials: {{"input_venue": "Canucks", "LoginId": "Tiwari8703", "password": "mypass"}}
- "Send email via Gmail API" -> node_type: "api", portal_name: "Gmail", api_action: "Send email using Gmail API", provided_credentials: null
- "How are you?" -> node_type: null, message: "I'm doing great! I can help you access your work portals and APIs. What would you like me to do?", provided_credentials: null
"""
    
    try:
        print(f"[Gemini Intent] Calling Gemini API with user message: {user_message}")
        print(f"[Gemini Intent] API Key loaded: {'Yes' if GEMINI_API_KEY else 'No'}")
        
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=system_prompt + f"\n\nUser message: {user_message}",
            config=types.GenerateContentConfig(
                response_mime_type='application/json',
                temperature=0.3
            )
        )
        
        print(f"[Gemini Intent] Raw response: {response.text}")
        result = json.loads(response.text)
        print(f"[Gemini Intent] Parsed: {json.dumps(result, indent=2)}")
        return result
        
    except json.JSONDecodeError as e:
        print(f"[Gemini Intent] JSON Parse Error: {e}")
        print(f"[Gemini Intent] Raw response was: {response.text if 'response' in locals() else 'No response'}")
        import traceback
        traceback.print_exc()
        return {
            "intent": "error",
            "node_type": None,
            "message": "I had trouble understanding that. Could you rephrase?"
        }
    except Exception as e:
        print(f"[Gemini Intent] Error: {e}")
        print(f"[Gemini Intent] Error type: {type(e).__name__}")
        import traceback
        traceback.print_exc()
        return {
            "intent": "error",
            "node_type": None,
            "message": "I had trouble understanding that. Could you rephrase?"
        }

def parse_tinyfish_result(result):
    """Parse TinyFish result into structured format - handles nested responses safely"""
    
    if not result:
        return {"status": "error", "reason": "Empty TinyFish result"}
    
    # Handle string results
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except:
            return {"status": "error", "reason": "Invalid JSON response"}
    
    # Handle non-dict results
    if not isinstance(result, dict):
        return {"status": "error", "reason": "TinyFish response is not a JSON object"}
    
    # Check if status is at top level
    if "status" in result:
        return result
    
    # Check if response is nested under "data" key
    if "data" in result and isinstance(result["data"], dict):
        if "status" in result["data"]:
            return result["data"]
    
    # Check if response is nested under "result" key
    if "result" in result and isinstance(result["result"], dict):
        if "status" in result["result"]:
            return result["result"]
    
    # No valid status found - return error
    return {"status": "error", "reason": "Unknown TinyFish response format - missing 'status' field"}

def format_result(data):
    """Format TinyFish result data for user display"""
    if isinstance(data, dict):
        return json.dumps(data, indent=2)
    elif isinstance(data, list):
        return json.dumps(data, indent=2)
    else:
        return str(data)

def execute_tinyfish_session_background(session_id, client):
    """
    Background execution wrapper for TinyFish.
    Runs in a separate thread and stores final result in session.
    """
    try:
        print(f"[Background] Starting TinyFish execution for session {session_id[:8]}")
        result = execute_tinyfish_session(session_id, client)
        
        # Store final response in session for polling retrieval
        if session_id in sessions:
            session = sessions[session_id]
            session['final_response_message'] = result.get('message')
            session['has_final_response'] = True
            persist_session(session_id, session)
            print(f"[Background] Stored final response for session {session_id[:8]}")
    except Exception as e:
        print(f"[Background] Error in execution: {e}")
        import traceback
        traceback.print_exc()
        
        # Store error in session
        if session_id in sessions:
            session = sessions[session_id]
            session['final_response_message'] = f"⚠️ Execution failed: {str(e)}"
            session['has_final_response'] = True
            session['live_status'] = 'error'
            persist_session(session_id, session)

def execute_tinyfish_session(session_id, client=None):
    """Execute TinyFish for a session and handle the result.
    Uses new router-based architecture.
    """
    if session_id not in sessions:
        return {"type": "text", "message": "Session not found"}
    
    session = sessions[session_id]
    
    # Track execution start time and set live status
    from datetime import datetime
    session['execution_started_at'] = datetime.now().isoformat()
    session['live_status'] = 'starting'
    session['live_message'] = 'Starting browser automation...'
    persist_session(session_id, session)
    print(f"[Live Status] session {session_id[:8]} -> starting")
    
    retry_count = session.get('retry_count', 0)
    print(f"[Execution] Starting TinyFish for session {session_id[:8]} (retry={retry_count})")
    
    # Check for execution blueprint from previous successful run
    node = saved_nodes.get(session.get("matched_node_id"))
    blueprint = None
    
    if node:
        blueprint = (node.get("metadata") or {}).get("execution_blueprint")
    
    # Build comprehensive execution goal
    base_goal = build_execution_goal_comprehensive(session)
    
    # Strengthen goal with blueprint if available
    if blueprint:
        print(f"[Blueprint] Using execution blueprint for repeat run (keyword={blueprint.get('success_keyword')})")
        goal = f"""
Complete the task fully.

Primary objective:
{blueprint.get("task_goal_template")}

Your mission is COMPLETE ONLY when:
1. You reach a page containing keyword: {blueprint.get("success_keyword")}
2. You extract structured {blueprint.get("success_keyword")} data
3. You return structured JSON

CRITICAL RULES:
- DO NOT continue exploring after reaching this page.
- DO NOT scroll endlessly.
- DO NOT click unrelated menus.
- If data is visible → extract immediately.
- Expected result signature: {blueprint.get("result_signature")}
- If unable to reach this state after login → return status "error"
- Maximum navigation steps: 12

Original goal context:
{base_goal}
"""
    else:
        goal = base_goal
    
    # Decrypt credentials for TinyFish
    credentials = session.get('credentials', {})
    decrypted_creds = {}
    for key, value in credentials.items():
        if isinstance(value, str) and value.startswith('gAAAAA'):
            try:
                decrypted_creds[key] = cipher.decrypt(value.encode()).decode()
            except:
                decrypted_creds[key] = value
        else:
            decrypted_creds[key] = value
    
    # Execute TinyFish
    portal_url = session.get('portal_url')
    
    try:
        tinyfish_result = run_tinyfish(portal_url, goal, decrypted_creds, session_id, session, blueprint)
        parsed_result = parse_tinyfish_result(tinyfish_result)
        
        result_status = parsed_result.get('status')
        print(f"[Execution] TinyFish returned status={result_status}")
        
        # Capture streaming_url from TinyFish result
        streaming_url = tinyfish_result.get('streaming_url') or parsed_result.get('streaming_url')
        if streaming_url:
            session['streaming_url'] = streaming_url
            print(f"[Execution] Captured streaming_url: {streaming_url}")
        
        # Handle timeout/error with retry logic
        # Reduce retry if blueprint exists (should succeed or fail fast)
        max_retries = 0 if blueprint else 1
        
        if result_status in ["timeout", "error"]:
            if retry_count < max_retries:
                session['retry_count'] = retry_count + 1
                persist_session(session_id, session)
                print(f"[Execution] Retry attempt {retry_count + 1} for status={result_status}")
                return execute_tinyfish_session(session_id, client)
            else:
                # Retry failed or blueprint-guided run failed
                print(f"[Execution] {'Blueprint-guided run' if blueprint else 'Retry'} failed, marking session as error")
                update_session_mode(session, 'error', 'error')
                session['retry_count'] = 0
                persist_session(session_id, session)
                
                error_msg = parsed_result.get('message', 'Unknown error')
                
                # Improved error messaging for blueprint-guided failures
                if blueprint:
                    message = f"⚠️ I tried using your previously successful navigation path but the portal did not reach the expected result page. The portal flow may have changed.\n\nError: {error_msg}"
                else:
                    message = f"⚠️ I couldn't complete the task automatically. The portal may be slow or blocked. Error: {error_msg}\n\nPlease try again."
                
                response = {
                    "type": "text",
                    "message": message,
                    "session_id": session_id
                }
                if streaming_url:
                    response['streaming_url'] = streaming_url
                return response
        
        # Handle result using result handler (pass client for Gemini interpretation)
        response = handle_execution_result(
            session,
            parsed_result,
            saved_nodes,
            persist_node,
            persist_session,
            client
        )
        
        # Clear retry count on success
        if result_status == 'complete':
            session['retry_count'] = 0
            persist_session(session_id, session)
        
        # Include streaming_url in response
        if streaming_url:
            response['streaming_url'] = streaming_url
        
        return response
        
    except Exception as e:
        print(f"[Execution] Error: {e}")
        import traceback
        traceback.print_exc()
        
        update_session_mode(session, 'error', 'error')
        persist_session(session_id, session)
        
        return {
            "type": "text",
            "message": f"I encountered an error while executing: {str(e)}",
            "session_id": session_id
        }

def run_orchestration_loop(session_id, task):
    """Mode-based orchestration for credential-first execution"""
    
    if session_id not in sessions:
        return {"type": "text", "message": "Session not found"}
    
    session = sessions[session_id]
    portal_url = session["portal_url"]
    credentials = session["credentials"]
    history = session["history"]
    mode = session.get("mode", "ready_to_run")
    retry_count = session.get("retry_count", 0)
    
    # Safety guards
    if retry_count > 2:
        session["status"] = "error"
        session["mode"] = "error"
        log_session_event(session_id, "Too many retries", {"retry_count": retry_count})
        return {
            "type": "text",
            "message": "I couldn't complete this task automatically. The portal may require manual verification or additional credentials.",
            "session_id": session_id
        }
    
    log_session_event(session_id, f"Mode: {mode}, Retry: {retry_count}", {"credentials": list(mask_credentials(credentials).keys())})
    
    # Check credential readiness
    is_ready, suggested_mode = determine_credential_readiness(session)
    
    if not is_ready and mode == "ready_to_run":
        # Not actually ready - should be collecting credentials
        session["mode"] = "collecting_credentials"
        mode = "collecting_credentials"
        log_session_event(session_id, "Not enough credentials, switching to collecting mode")
    
    # If in collecting_credentials mode, don't execute yet
    if mode == "collecting_credentials":
        log_session_event(session_id, "In collecting_credentials mode, waiting for user input")
        return {
            "type": "text",
            "message": "I need your credentials before I can proceed. Please provide your login information.",
            "session_id": session_id
        }
    
    # Build single comprehensive execution goal
    goal = build_execution_goal(session)
    
    log_session_event(session_id, "TinyFish goal", {"preview": goal[:200]})
    
    # Decrypt credentials for TinyFish
    decrypted_creds = {}
    for key, value in credentials.items():
        if isinstance(value, str) and value.startswith('gAAAAA'):
            try:
                decrypted_creds[key] = cipher.decrypt(value.encode()).decode()
            except:
                decrypted_creds[key] = value
        else:
            decrypted_creds[key] = value
    
    # Update mode to running
    session["mode"] = "running"
    session["status"] = "running"
    
    # Run TinyFish for full execution
    result = run_tinyfish(portal_url, goal, decrypted_creds, session_id)
    
    # Parse result safely
    parsed = parse_tinyfish_result(result)
    history.append(parsed)
    session["history"] = history
    
    # Extract status
    status = parsed.get("status")
    log_session_event(session_id, f"TinyFish status: {status}")
    
    if not status:
        session["retry_count"] = retry_count + 1
        session["mode"] = "error"
        session["status"] = "error"
        return {
            "type": "text",
            "message": "I encountered an error. Please try again.",
            "session_id": session_id
        }
    
    # Handle execution result
    new_mode, new_status, should_save_node, response_data = handle_tinyfish_execution_result(session, parsed)
    
    # Update session state
    session["mode"] = new_mode
    session["status"] = new_status
    
    log_session_event(session_id, f"Mode transition: {mode} → {new_mode}")
    
    # Save/update node if successful
    if should_save_node:
        portal_name = session.get("portal_name")
        existing_portal = None
        for pid, node in saved_nodes.items():
            if node.get("portal_url") == portal_url:
                existing_portal = pid
                break
        
        if not existing_portal:
            portal_id = str(uuid.uuid4())
            node_portal_key = normalize_portal_key(portal_name, portal_url)
            saved_nodes[portal_id] = {
                "type": "browser",
                "portal_name": portal_name,
                "portal_url": portal_url,
                "portal_key": node_portal_key,
                "node_type": "browser",
                "credentials": credentials,
                "metadata": {}
            }
            persist_node(portal_id, saved_nodes[portal_id])
            log_session_event(session_id, f"Created new node: {portal_id} (key={node_portal_key})")
        else:
            saved_nodes[existing_portal]["credentials"] = credentials
            # Update portal_key if missing
            if not saved_nodes[existing_portal].get("portal_key"):
                saved_nodes[existing_portal]["portal_key"] = normalize_portal_key(
                    saved_nodes[existing_portal].get("portal_name"),
                    saved_nodes[existing_portal].get("portal_url")
                )
            persist_node(existing_portal, saved_nodes[existing_portal])
            log_session_event(session_id, f"Updated node: {existing_portal}")
        
        # Format result data if present
        if response_data.get("data"):
            formatted_data = format_result(response_data["data"])
            response_data["message"] = f"✅ {response_data.get('message', 'Task completed!')}\n\n{formatted_data}"
    
    # Reset retry count on success or waiting_extra_input
    if new_mode in ["complete", "waiting_extra_input"]:
        session["retry_count"] = 0
    elif new_mode == "error":
        session["retry_count"] = retry_count + 1
    
    # Add session_id and streaming_url to response
    response_data["session_id"] = session_id
    response_data["streaming_url"] = session.get("streaming_url")
    
    # Persist session state
    persist_session(session_id, session)
    
    return response_data

def discover_login_fields(portal_url):
    """Phase 1: TinyFish visits portal and discovers login fields"""
    
    discovery_goal = """Visit this login page and identify ALL input fields in the login form.
Look for ALL fields including:
- Text inputs (username, email, account number, etc.)
- Password inputs
- Hidden fields that may be required
- Any dropdowns or selects

Inspect the HTML form carefully. Return a JSON array with ALL fields found, in this exact format:
[
  {"field": "exact_field_name_or_id", "label": "Human Readable Label", "type": "text|password|email|number"}
]

Use the actual field name/id attribute from the HTML. Return ONLY the JSON array, nothing else."""
    
    print(f"[TinyFish Discovery] Discovering login fields for {portal_url}")
    
    headers = {
        "X-API-Key": TINYFISH_API_KEY,
        "Content-Type": "application/json",
        "Accept": "text/event-stream"
    }
    
    payload = {
        "url": portal_url,
        "goal": discovery_goal
    }
    
    # Default fallback fields
    default_fields = [
        {"field": "username", "label": "Username", "type": "text"},
        {"field": "password", "label": "Password", "type": "password"}
    ]
    
    try:
        response = requests.post(
            "https://agent.tinyfish.ai/v1/automation/run-sse",
            json=payload,
            headers=headers,
            stream=True,
            timeout=120
        )
        
        print(f"[TinyFish Discovery] HTTP {response.status_code}")
        response.raise_for_status()
        
        result_text = ""
        streaming_url = None
        
        for line in response.iter_lines(decode_unicode=True):
            if not line:
                continue
            
            if line.startswith("data: "):
                data_str = line[6:]
                try:
                    event = json.loads(data_str)
                    event_type = event.get("type", "unknown")
                    
                    print(f"[TinyFish Discovery] Event: {event_type}")
                    
                    # Capture streaming URL
                    if event_type == "STREAMING_URL":
                        # Log raw payload for debugging
                        print(f"[TinyFish Discovery] STREAMING_URL raw payload: {json.dumps(event)}")
                        
                        # Extract URL with fallbacks (TinyFish docs use "streaming_url")
                        streaming_url = (
                            event.get("streaming_url")
                            or event.get("streamingUrl")
                            or event.get("url")
                            or (event.get("data") or {}).get("streaming_url")
                            or (event.get("data") or {}).get("streamingUrl")
                        )
                        
                        print(f"[TinyFish Discovery] Resolved streaming URL: {streaming_url}")
                    
                    if event_type == "result":
                        result_text = event.get("data", "") or event.get("result", "")
                    elif event_type == "COMPLETE":
                        raw = event.get("resultJson") or event.get("result") or event.get("data", {})
                        
                        # If raw is a string, parse it
                        if isinstance(raw, str):
                            try:
                                raw = json.loads(raw)
                            except:
                                pass
                        
                        # Unwrap nested "result" key
                        if isinstance(raw, dict):
                            fields_data = raw.get("result") or raw.get("fields") or raw.get("data")
                        elif isinstance(raw, list):
                            fields_data = raw
                        else:
                            fields_data = None
                        
                        # Validate it's a proper fields list
                        if isinstance(fields_data, list) and len(fields_data) > 0:
                            if isinstance(fields_data[0], dict) and "field" in fields_data[0]:
                                return fields_data
                        
                        result_text = json.dumps(raw) if raw else ""
                    elif event_type in ("COMPLETED", "done", "complete", "completed"):
                        result_text = event.get("resultJson", "") or event.get("result", "") or event.get("data", "") or result_text
                        
                except json.JSONDecodeError:
                    pass
        
        result_str = str(result_text) if result_text else ""
        print(f"[TinyFish Discovery] Raw result: {result_str[:300]}")
        
        # Try to parse as JSON array
        if result_text:
            try:
                fields = json.loads(result_text)
                if isinstance(fields, list) and len(fields) > 0:
                    print(f"[TinyFish Discovery] Found {len(fields)} fields")
                    return fields
            except json.JSONDecodeError:
                print(f"[TinyFish Discovery] Failed to parse result as JSON, using defaults")
        
        print(f"[TinyFish Discovery] No valid fields found, using defaults")
        return default_fields
        
    except Exception as e:
        print(f"[TinyFish Discovery] Error: {e}")
        import traceback
        traceback.print_exc()
        print(f"[TinyFish Discovery] Falling back to default fields")
        return default_fields

def run_tinyfish(url, instruction, credentials, job_id=None, session=None, blueprint=None):
    """Execute TinyFish automation with credential replacement and live status updates"""
    
    session_id = session.get('session_id') if session else None
    
    # Replace credential placeholders in instruction
    filled_instruction = instruction
    for key, value in credentials.items():
        placeholder = f"{{{key}}}"
        # Mask passwords in logs
        display_value = "****" if "password" in key.lower() else str(value)
        filled_instruction = filled_instruction.replace(placeholder, str(value))
    
    # Append structured JSON response format instructions
    structured_response_format = """

IMPORTANT RESPONSE RULES:
You must return a JSON object in one of these exact formats:

If you completed the task successfully:
{
  "status": "complete",
  "data": <extracted data here>
}

If you need user input to proceed (hit a field you don't have a value for):
{
  "status": "needs_input",
  "field_needed": "exact_field_name",
  "label": "Human readable label",
  "type": "text|password|email|number",
  "message": "Please provide your X to continue",
  "page_description": "brief description of current page state"
}

If you completed this step and need to report what you found for the next step:
{
  "status": "next_step",
  "page_description": "description of current page state",
  "fields_found": [{"field": "name", "label": "Label", "type": "text"}],
  "data_collected": <any data collected so far>
}

If you encountered an error:
{
  "status": "error",
  "reason": "description of what went wrong"
}

NEVER return plain text. ALWAYS return one of these JSON formats."""
    
    filled_instruction = filled_instruction + structured_response_format
    
    print(f"[TinyFish] Executing instruction: {filled_instruction[:200]}...")
    print(f"[TinyFish] URL: {url}")
    if job_id:
        print(f"[TinyFish] Job ID: {job_id}")
    
    headers = {
        "X-API-Key": TINYFISH_API_KEY,
        "Content-Type": "application/json",
        "Accept": "text/event-stream"
    }
    
    payload = {
        "url": url,
        "goal": filled_instruction
    }
    
    try:
        response = requests.post(
            "https://agent.tinyfish.ai/v1/automation/run-sse",
            json=payload,
            headers=headers,
            stream=True,
            timeout=300
        )
        
        print(f"[TinyFish] HTTP {response.status_code}")
        response.raise_for_status()
        
        result_text = ""
        streaming_url = None
        all_events = []
        completed = False
        
        # Start timeout tracking
        import time
        start_time = time.time()
        
        # Success detection for blueprint-guided runs
        success_state_detected = False
        last_progress_at = time.time()
        
        # Reduce timeout when blueprint exists (should be fast or fail fast)
        TIMEOUT_SECONDS = 45 if blueprint else 90
        print(f"[TinyFish] Starting execution with {TIMEOUT_SECONDS}s timeout{' (blueprint mode)' if blueprint else ''}")
        
        for line in response.iter_lines(decode_unicode=True):
            # Check timeout
            elapsed = time.time() - start_time
            if elapsed > TIMEOUT_SECONDS:
                # Log extraction silence if success state was detected
                if success_state_detected:
                    print(f"[TinyFish Guard] Timeout occurred AFTER success state — extraction failure")
                
                print(f"[TinyFish] Execution timeout reached after {elapsed:.1f}s")
                
                # Update live status to timeout
                if session and session_id:
                    session['live_status'] = 'timeout'
                    session['live_message'] = 'Execution timed out'
                    persist_session(session_id, session)
                    print(f"[Live Status] session {session_id[:8]} -> timeout")
                
                return {
                    "status": "timeout",
                    "message": "TinyFish execution exceeded timeout",
                    "streaming_url": streaming_url
                }
            if not line:
                continue
            
            if line.startswith("data: "):
                data_str = line[6:]
                try:
                    event = json.loads(data_str)
                    event_type = event.get("type", "unknown")
                    all_events.append(event_type)
                    
                    print(f"[TinyFish] Event: {event_type}")
                    
                    # Capture streaming URL
                    if event_type == "STREAMING_URL":
                        # Log raw payload for debugging
                        print(f"[TinyFish] STREAMING_URL raw payload: {json.dumps(event)}")
                        
                        # Extract URL with fallbacks (TinyFish docs use "streaming_url")
                        streaming_url = (
                            event.get("streaming_url")
                            or event.get("streamingUrl")
                            or event.get("url")
                            or (event.get("data") or {}).get("streaming_url")
                            or (event.get("data") or {}).get("streamingUrl")
                        )
                        
                        print(f"[TinyFish] Resolved streaming URL: {streaming_url}")
                        
                        # IMMEDIATELY persist streaming_url to session for live preview
                        if session and session_id:
                            session['streaming_url'] = streaming_url
                            session['live_status'] = 'running'
                            session['live_message'] = 'Live preview available'
                            persist_session(session_id, session)
                            print(f"[TinyFish] Persisted streaming_url to session {session_id[:8]}")
                            print(f"[Live Status] session {session_id[:8]} -> running")
                        
                        if job_id and job_id in jobs:
                            jobs[job_id]['streaming_url'] = streaming_url
                            print(f"[TinyFish] Streaming URL saved immediately to job {job_id}: {streaming_url}")
                        else:
                            print(f"[TinyFish] Streaming URL: {streaming_url}")
                    
                    # Detect success keyword in STREAMING/PROGRESS events
                    if event_type in ["PROGRESS", "STREAMING", "STREAMING_URL"]:
                        event_blob = json.dumps(event).lower()
                        
                        if blueprint and blueprint.get("success_keyword"):
                            keyword = blueprint["success_keyword"].lower()
                            
                            if keyword in event_blob:
                                success_state_detected = True
                                last_progress_at = time.time()
                                print(f"[TinyFish Guard] SUCCESS STATE DETECTED via keyword '{keyword}'")
                    
                    # Update live status on PROGRESS events
                    if event_type == "PROGRESS" and session:
                        if not session.get('streaming_url'):
                            session['live_status'] = 'starting'
                        else:
                            session['live_status'] = 'running'
                        if session_id:
                            persist_session(session_id, session)
                    
                    # Extraction forcing window - prevent wandering after success
                    if success_state_detected:
                        elapsed_since_success = time.time() - last_progress_at
                        
                        if elapsed_since_success > 10:
                            print("[TinyFish Guard] Success page reached but no extraction — forcing completion")
                            
                            # Update live status to error
                            if session and session_id:
                                session['live_status'] = 'error'
                                session['live_message'] = 'Reached expected page but no extraction'
                                persist_session(session_id, session)
                            
                            return {
                                "status": "error",
                                "message": "Reached expected page but TinyFish did not extract structured result",
                                "streaming_url": streaming_url
                            }
                    
                    if event_type == "result":
                        result_text = event.get("data", "") or event.get("result", "")
                    elif event_type in ("COMPLETE", "COMPLETED", "done", "complete", "completed"):
                        result_text = event.get("resultJson", "") or event.get("result", "") or event.get("data", "") or result_text
                        completed = True
                        print(f"[TinyFish] COMPLETE event received after {time.time() - start_time:.1f}s")
                        
                        # Update live status to complete
                        if session and session_id:
                            session['live_status'] = 'complete'
                            session['live_message'] = 'Execution complete'
                            persist_session(session_id, session)
                            print(f"[Live Status] session {session_id[:8]} -> complete")
                        
                except json.JSONDecodeError:
                    pass
        
        # Check if we got a COMPLETE event
        if not completed:
            elapsed = time.time() - start_time
            print(f"[TinyFish] Stream ended without COMPLETE event after {elapsed:.1f}s")
            
            # Update live status to timeout
            if session and session_id:
                session['live_status'] = 'timeout'
                session['live_message'] = 'Execution did not complete'
                persist_session(session_id, session)
                print(f"[Live Status] session {session_id[:8]} -> timeout")
            
            return {
                "status": "timeout",
                "message": "TinyFish execution did not complete",
                "streaming_url": streaming_url,
                "events": all_events
            }
        
        # Safe logging - handle both dict and string results
        if isinstance(result_text, dict):
            print(f"[TinyFish] Result: {json.dumps(result_text)[:200]}")
        elif isinstance(result_text, str):
            print(f"[TinyFish] Result: {result_text[:200]}")
        else:
            print(f"[TinyFish] Result: {str(result_text)[:200]}")
        
        # Return both result and streaming URL
        parsed_result = None
        if isinstance(result_text, dict):
            parsed_result = result_text
        elif isinstance(result_text, str) and result_text:
            try:
                parsed_result = json.loads(result_text)
            except:
                parsed_result = {"raw": result_text}
        else:
            parsed_result = {"raw": str(result_text), "events": all_events}
        
        # Always include streaming_url in the result
        if streaming_url:
            parsed_result["streaming_url"] = streaming_url
            print(f"[TinyFish] Including streaming_url in result: {streaming_url}")
        
        return parsed_result
        
    except Exception as e:
        print(f"[TinyFish] Error: {e}")
        import traceback
        traceback.print_exc()
        
        # Update live status to error
        if session and session_id:
            session['live_status'] = 'error'
            session['live_message'] = f'Execution error: {str(e)}'
            persist_session(session_id, session)
            print(f"[Live Status] session {session_id[:8]} -> error")
        
        return {
            "status": "error",
            "message": str(e),
            "streaming_url": None
        }

@app.route('/navi/portal', methods=['POST'])
def save_portal_data():
    data = request.json
    portal = data.get('portal')
    shifts = data.get('shifts', [])
    timestamp = data.get('timestamp')
    
    navi_portals[portal] = {
        'shifts': shifts,
        'timestamp': timestamp,
        'last_updated': timestamp
    }
    
    return jsonify({
        'success': True,
        'count': len(shifts)
    })

@app.route('/navi/portals', methods=['GET'])
def get_portals():
    return jsonify({
        'portals': navi_portals
    })

@app.route('/navi/shifts/extension', methods=['GET'])
def get_all_shifts():
    all_shifts = []
    
    for portal_name, portal_data in navi_portals.items():
        for shift in portal_data.get('shifts', []):
            shift_copy = shift.copy()
            shift_copy['portal'] = portal_name
            all_shifts.append(shift_copy)
    
    all_shifts.sort(key=lambda x: x.get('date', ''))
    
    return jsonify({
        'shifts': all_shifts
    })

@app.route('/navi/action', methods=['POST'])
def trigger_action():
    """Trigger TinyFish agent to execute an action on a portal"""
    try:
        from agents.tinyfish_helper import run_agent
        
        data = request.json
        goal = data.get('goal', '')
        url = data.get('url', '')
        
        if not goal:
            return jsonify({
                'success': False,
                'error': 'No goal provided'
            }), 400
        
        # Run TinyFish agent with the goal
        result = run_agent(url=url, goal=goal, max_steps=10)
        
        return jsonify({
            'success': True,
            'result': result,
            'message': 'Action completed successfully'
        }), 200
        
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.route('/navi/page-data', methods=['POST'])
def save_page_data():
    """Receives page data from browser automation and stores it"""
    try:
        data = request.json
        portal = data.get('portal')
        url = data.get('url')
        text = data.get('text', '')
        title = data.get('title', '')
        
        # Store page data in memory
        page_data_key = f'page_data_{portal}' if portal else 'page_data_generic'
        if portal not in navi_portals:
            navi_portals[portal] = {}
        
        navi_portals[portal]['page_data'] = {
            'url': url,
            'title': title,
            'text': text,
            'timestamp': datetime.now().isoformat()
        }
        
        # Send to Gemini for summarization
        try:
            summary_prompt = f"Summarize this page data briefly:\n\nTitle: {title}\nURL: {url}\n\nContent:\n{text[:2000]}"
            
            response = client.models.generate_content(
                model='models/gemini-2.5-flash',
                contents=summary_prompt,
                config=types.GenerateContentConfig(
                    max_output_tokens=500
                )
            )
            summary = response.text
        except Exception as e:
            print(f"[Page Data] Summarization error: {e}")
            summary = "Page data received"
        
        return jsonify({
            'success': True,
            'summary': summary,
            'data': {
                'portal': portal,
                'title': title,
                'url': url
            }
        }), 200
        
    except Exception as e:
        print(f"[Page Data] Error: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.route('/navi/extract', methods=['POST'])
def extract():
    """Receives HTML from content script, uses TinyFish to extract data, then Gemini to synthesize"""
    try:
        from agents.tinyfish_helper import extract_from_html
        
        data = request.json
        html = data.get('html', '')
        url = data.get('url', '')
        portal = data.get('portal', 'unknown')
        objective = data.get('objective', 'extract data')
        original_question = data.get('original_question', '')
        
        if not html:
            return jsonify({
                'success': False,
                'error': 'No HTML provided'
            }), 400
        
        print(f"[Navi Extract] Portal: {portal}, Objective: {objective}")
        print(f"[Navi Extract] HTML length: {len(html)}")
        print(f"[Navi Extract] Original question: {original_question}")
        
        # STEP 1 - Use TinyFish to extract data from HTML
        extraction_goal = f"Extract schedule data from this HTML page. {objective}. Return structured data with dates, times, and shift details."
        
        print(f"[Navi Extract] Calling TinyFish with goal: {extraction_goal[:200]}")
        
        tinyfish_result = extract_from_html(
            html=html,
            goal=extraction_goal,
            max_steps=5
        )
        
        print(f"[Navi Extract] TinyFish result: {str(tinyfish_result)[:500]}")
        
        # STEP 2 - Send extracted data to Gemini for synthesis
        synthesis_prompt = f"""You received this extracted data from the {portal} portal:

{json.dumps(tinyfish_result, indent=2)}

The user's original question was: "{original_question}"

Synthesize this data into a brief, natural language answer. Be conversational and direct. Focus on answering the user's specific question."""
        
        response = client.models.generate_content(
            model='models/gemini-2.5-flash',
            contents=synthesis_prompt,
            config=types.GenerateContentConfig(
                max_output_tokens=500,
                temperature=0.7
            )
        )
        
        summary = response.text
        print(f"[Navi Extract] Gemini synthesis: {summary[:200]}")
        
        return jsonify({
            'success': True,
            'summary': summary,
            'raw_data': tinyfish_result
        }), 200
        
    except Exception as e:
        print(f"[Navi Extract] Error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.route('/navi/synthesize', methods=['POST'])
def synthesize():
    """Receives raw extracted data from content scripts and synthesizes natural language summary"""
    try:
        data = request.json
        raw_data = data.get('raw_data', '')
        original_question = data.get('original_question', '')
        portal = data.get('portal', 'unknown')
        
        print(f"[Navi Synthesize] Portal: {portal}, Question: {original_question}")
        
        # Send to Gemini for synthesis
        synthesis_prompt = f"""You received this raw data from the {portal} portal:

{raw_data}

The user's original question was: "{original_question}"

Synthesize this data into a brief, natural language answer. Be conversational and direct. Don't mention that you extracted data or used any tools."""
        
        response = client.models.generate_content(
            model='models/gemini-2.5-flash',
            contents=synthesis_prompt,
            config=types.GenerateContentConfig(
                max_output_tokens=500,
                temperature=0.7
            )
        )
        
        summary = response.text
        print(f"[Navi Synthesize] Summary: {summary[:200]}")
        
        return jsonify({
            'success': True,
            'summary': summary
        }), 200
        
    except Exception as e:
        print(f"[Navi Synthesize] Error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.route('/navi/chat', methods=['POST'])
def chat():
    """Main chat endpoint - Gemini planning only, returns JSON commands to frontend"""
    try:
        data = request.json
        user_message = data.get('message', '')
        page_context = data.get('page_context', {})
        
        if not user_message:
            return jsonify({'response': 'Please send a message.'}), 400
        
        # STEP 1 - Load memory from cache
        memory = {'portals': {}, 'shifts': []}
        
        # Get shifts from extension portals
        for portal_name, portal_data in navi_portals.items():
            memory['portals'][portal_name] = portal_data
            memory['shifts'].extend(portal_data.get('shifts', []))
        
        # Legacy ABI scheduler cache removed - now using credential-first execution only
        
        # STEP 2 - Build prompt for Gemini
        today = datetime.now().strftime('%Y-%m-%d')
        memory_json = json.dumps(memory, indent=2)
        page_json = json.dumps(page_context, indent=2)
        
        system_prompt = """You are Navi, a personal web agent that helps users navigate portals and manage their digital life.

You have four modes:

MODE 1 - Answer from memory:
If the user asks something you already know from stored data - answer directly.
Be conversational. Be brief.

MODE 2 - Fetch live data:
If user asks something requiring fresh portal data respond with EXACTLY:
[FETCH]: {{"portal": "abi", "action": "navigate_and_extract", "url": "https://ess.abimm.com/ABIMM_ASP/Request.aspx", "objective": "extract_schedule"}}

Available portals:
- "abi" = ABI MasterMind (https://ess.abimm.com/ABIMM_ASP/Request.aspx)
- "ukg" = UKG Workforce (https://workforce.ukg.com)

MODE 3 - Execute action:
If user asks to DO something on a portal (click, fill form, submit):
[ACTION]: {{"portal": "abi", "action": "click_element", "url": "current page url", "objective": "specific action description"}}

MODE 4 - Navigate in browser:
If user asks to OPEN or GO TO a portal or website in their browser respond with:
[NAVIGATE]: {{"portal": "abi", "url": "https://ess.abimm.com/ABIMM_ASP/Request.aspx"}}

This opens the portal in Chrome right in front of the user.
Use this when user says: "open my ABI", "go to ABI", "show me my schedule on screen", "open my account", "navigate to"

RULES:
- Never mention TinyFish or Gemini
- Never mention AI or models
- You are Navi, a personal agent
- Be direct and conversational
- Under 3 sentences for simple answers
- Always check memory before fetching
- For [FETCH] and [ACTION], always include: portal, action, url, objective

CURRENT MEMORY:
{memory}

CURRENT PAGE:
{page}

TODAY: {today}""".format(memory=memory_json, page=page_json, today=today)
        
        # STEP 3 - Send to Gemini
        response = client.models.generate_content(
            model='models/gemini-2.5-flash',
            contents=system_prompt + "\n\nUser: " + user_message,
            config=types.GenerateContentConfig(
                max_output_tokens=1000,
                temperature=0.7
            )
        )
        response_text = response.text
        
        print("[Navi Chat] User:", user_message)
        print("[Navi Chat] Gemini response:", response_text[:200])
        
        # STEP 4 - Detect command tags and return them to frontend
        # Backend is BRAIN ONLY - it never executes browser actions
        # Frontend will handle all browser manipulation
        
        print("[Navi Brain] Returning response to frontend:", response_text[:200])
        
        # STEP 5 - Return response with ALL command tags intact
        # Frontend will parse [FETCH], [ACTION], [NAVIGATE] and execute them
        return jsonify({'response': response_text}), 200
        
    except Exception as e:
        print("[Navi Chat] Error:", e)
        import traceback
        traceback.print_exc()
        return jsonify({
            'response': 'Sorry, I encountered an error. Please try again.'
        }), 500

# Legacy scheduler removed - using credential-first execution only

# ===== SAAS PLATFORM API ROUTES =====

@app.route('/api/chat', methods=['POST'])
def api_chat():
    """Router-based chat endpoint - clean architecture with session-aware routing"""
    try:
        data = request.json
        user_message = data.get('message', '')
        
        if not user_message:
            return jsonify({'error': 'No message provided'}), 400
        
        print(f"\n[API Chat] User message: {user_message}")
        
        # ===== STEP 0: SESSION WATCHDOG - Check for stuck sessions =====
        from datetime import datetime, timedelta
        for sid, sess in list(sessions.items()):
            if sess.get('execution_started_at'):
                started_at = datetime.fromisoformat(sess['execution_started_at'])
                elapsed = datetime.now() - started_at
                
                # If session running longer than 2 minutes, auto-terminate
                if elapsed > timedelta(minutes=2):
                    print(f"[Watchdog] Session {sid[:8]} stuck for {elapsed.total_seconds():.0f}s, terminating")
                    sess['mode'] = 'error'
                    sess['status'] = 'error'
                    sess['execution_started_at'] = None
                    persist_session(sid, sess)
        
        # ===== STEP 1: ROUTE MESSAGE =====
        route_result = route_message(user_message, sessions, saved_nodes, edges_storage)
        route = route_result['route']
        session_id = route_result.get('session_id')
        matched_node_id = route_result.get('matched_node_id')
        
        print(f"[Router] route={route}, session_id={session_id[:8] if session_id else None}, matched_node_id={matched_node_id[:8] if matched_node_id else None}")
        
        # ===== STEP 1: HANDLE ACTIVE SESSION INPUT =====
        if route == 'active_session_input':
            session = sessions[session_id]
            
            # Extract credentials from natural language
            required_fields = session.get('required_fields', [])
            extracted_creds = extract_session_input(user_message, required_fields, client)
            
            if not extracted_creds:
                return jsonify({
                    'type': 'text',
                    'message': 'I didn\'t catch any login details. Could you provide them? For example: "username is john, password is secret123"',
                    'session_id': session_id
                }), 200
            
            # Update session credentials
            update_session_credentials(session, extracted_creds, cipher)
            
            # Evaluate readiness
            readiness = evaluate_session_readiness(session)
            
            if readiness['is_ready']:
                # Ready to execute
                update_session_mode(session, 'ready', 'running')
                persist_session(session_id, session)
                
                # Start TinyFish execution in background thread
                thread = threading.Thread(
                    target=execute_tinyfish_session_background,
                    args=(session_id, client)
                )
                thread.daemon = True
                thread.start()
                
                # Return immediately - frontend will poll for status
                return jsonify({
                    'type': 'text',
                    'message': 'Navi is working on your task...',
                    'session_id': session_id,
                    'running': True
                }), 200
            else:
                # Still need more
                missing = get_missing_field_names(session)
                session['missing_fields'] = readiness['missing_fields']
                persist_session(session_id, session)
                
                if missing:
                    message = f"Thanks! I still need: {', '.join(missing)}. Please provide them."
                else:
                    message = "I need a bit more information. Could you provide your login credentials?"
                
                return jsonify({
                    'type': 'text',
                    'message': message,
                    'session_id': session_id
                }), 200
        
        # ===== STEP 2: HANDLE FOLLOW-UP REASONING =====
        if route == 'followup_reasoning':
            from result_reasoning import reason_over_previous_result
            
            session = sessions[session_id]
            
            # Use Gemini to reason over stored result
            reasoning_result = reason_over_previous_result(user_message, session, client)
            answer = reasoning_result.get('answer')
            needs_refresh = reasoning_result.get('needs_refresh', False)
            
            # If data is stale or missing, auto-trigger refresh
            if needs_refresh:
                print(f"[Follow-up] Data stale/missing - auto-triggering refresh")
                
                # Check if we have a matched node to reuse credentials
                matched_node_id = session.get('matched_node_id')
                if matched_node_id and matched_node_id in saved_nodes:
                    node = saved_nodes[matched_node_id]
                    
                    # Create new session for refresh
                    new_session_id = str(uuid.uuid4())
                    refresh_session = create_session(
                        new_session_id,
                        node.get('portal_key'),
                        node.get('portal_name'),
                        node.get('portal_url'),
                        user_message,
                        node_type=node.get('type', 'browser'),
                        matched_node_id=matched_node_id
                    )
                    
                    # Reuse saved credentials
                    saved_creds = node.get('credentials', {}) or {}
                    update_session_credentials(refresh_session, saved_creds, cipher)
                    sessions[new_session_id] = refresh_session
                    
                    # Check readiness
                    readiness = evaluate_session_readiness(refresh_session)
                    
                    if readiness['is_ready']:
                        print(f"[SESSION START] Auto-refresh for {node.get('portal_name')}")
                        update_session_mode(refresh_session, 'ready', 'running')
                        persist_session(new_session_id, refresh_session)
                        
                        # Start TinyFish in background
                        thread = threading.Thread(
                            target=execute_tinyfish_session_background,
                            args=(new_session_id, client)
                        )
                        thread.daemon = True
                        thread.start()
                        
                        return jsonify({
                            'type': 'text',
                            'message': 'Let me refresh the schedule for you.',
                            'session_id': new_session_id,
                            'running': True
                        }), 200
            
            # Return answer from stored data
            if answer:
                return jsonify({
                    'type': 'text',
                    'message': answer,
                    'session_id': session_id
                }), 200
            else:
                return jsonify({
                    'type': 'text',
                    'message': 'Let me fetch that information for you.',
                    'session_id': session_id
                }), 200
        
        # ===== STEP 3: HANDLE GENERAL CHAT =====
        if route == 'general_chat':
            # Extract intent to get conversational response
            intent_result = extract_task_intent(user_message, saved_nodes, client)
            message = intent_result.get('message', 'I\'m here to help you access portals and fetch data. What would you like me to do?')
            
            return jsonify({
                'type': 'text',
                'message': message
            }), 200
        
        # ===== STEP 3: HANDLE REPEAT RUN =====
        if route == 'repeat_run':
            # Load matched node
            node = saved_nodes[matched_node_id]
            print(f"[PORTAL REUSED] {node.get('portal_name')} (node {matched_node_id[:8]})")
            
            # Extract intent to get any provided credentials
            intent_result = extract_task_intent(user_message, saved_nodes, client)
            provided_creds = intent_result.get('provided_credentials', {}) or {}
            
            # Create session with saved credentials
            new_session_id = str(uuid.uuid4())
            session = create_session(
                new_session_id,
                node.get('portal_key'),
                node.get('portal_name'),
                node.get('portal_url'),
                user_message,
                node_type=node.get('type', 'browser'),
                matched_node_id=matched_node_id
            )
            
            # Merge saved + provided credentials
            saved_creds = node.get('credentials', {}) or {}
            update_session_credentials(session, saved_creds, cipher)
            update_session_credentials(session, provided_creds, cipher)
            
            sessions[new_session_id] = session
            
            # Evaluate readiness
            readiness = evaluate_session_readiness(session)
            
            if readiness['is_ready']:
                print(f"[SESSION START] Repeat run for {node.get('portal_name')} with saved credentials")
                # Ready to execute
                update_session_mode(session, 'ready', 'running')
                persist_session(new_session_id, session)
                
                # Start TinyFish execution in background thread
                print(f"[TINYFISH RUNNING] Session {new_session_id[:8]}")
                thread = threading.Thread(
                    target=execute_tinyfish_session_background,
                    args=(new_session_id, client)
                )
                thread.daemon = True
                thread.start()
                
                # Return immediately - frontend will poll for status
                return jsonify({
                    'type': 'text',
                    'message': f'Let me fetch the latest {node.get("portal_name", "data")} for you.',
                    'session_id': new_session_id,
                    'running': True
                }), 200
            else:
                # Need more credentials
                update_session_mode(session, 'collecting', 'waiting_input')
                session['missing_fields'] = readiness['missing_fields']
                persist_session(new_session_id, session)
                
                missing = get_missing_field_names(session)
                if missing:
                    message = f"I need: {', '.join(missing)}. Please provide them."
                else:
                    message = "I need your credentials for this portal. You can provide them naturally."
                
                return jsonify({
                    'type': 'text',
                    'message': message,
                    'session_id': new_session_id
                }), 200
        
        # ===== STEP 4: HANDLE NEW TASK =====
        if route == 'new_task':
            # Extract full intent
            intent_result = extract_task_intent(user_message, saved_nodes, client)
            
            portal_name = intent_result.get('portal_name', '')
            portal_url = intent_result.get('portal_url')
            node_type = intent_result.get('node_type')
            provided_creds = intent_result.get('provided_credentials', {}) or {}
            
            if not portal_url or not node_type:
                return jsonify({
                    'type': 'text',
                    'message': 'I need more details about which portal to access. Could you provide the portal URL?'
                }), 200
            
            # Compute portal key
            portal_key = normalize_portal_key(portal_name, portal_url)
            
            # Create new session
            new_session_id = str(uuid.uuid4())
            session = create_session(
                new_session_id,
                portal_key,
                portal_name,
                portal_url,
                user_message,
                node_type=node_type
            )
            
            # Add provided credentials
            update_session_credentials(session, provided_creds, cipher)
            
            sessions[new_session_id] = session
            
            # Evaluate readiness
            readiness = evaluate_session_readiness(session)
            
            if readiness['is_ready']:
                # Ready to execute immediately
                update_session_mode(session, 'ready', 'running')
                persist_session(new_session_id, session)
                
                # Start TinyFish execution in background thread
                thread = threading.Thread(
                    target=execute_tinyfish_session_background,
                    args=(new_session_id, client)
                )
                thread.daemon = True
                thread.start()
                
                # Return immediately - frontend will poll for status
                return jsonify({
                    'type': 'text',
                    'message': 'Navi is working on your task...',
                    'session_id': new_session_id,
                    'running': True
                }), 200
            else:
                # Need credentials - try discovery
                update_session_mode(session, 'collecting', 'waiting_input')
                
                discovered_fields = []
                try:
                    discovery_goal = build_discovery_goal(portal_url)
                    discovery_result = run_tinyfish(portal_url, discovery_goal, {}, new_session_id)
                    parsed = parse_tinyfish_result(discovery_result)
                    
                    if parsed.get('status') == 'complete':
                        fields = parsed.get('data', {}).get('fields', [])
                        if fields:
                            normalized_fields = normalize_fields(fields)
                            discovered_fields = normalized_fields
                            set_required_fields(session, normalized_fields)
                except Exception as e:
                    print(f"[New Task] Discovery failed: {e}")
                
                # Set generic fields if discovery didn't work
                if not discovered_fields:
                    generic_fields = [
                        {"field": "username", "label": "Username", "type": "text"},
                        {"field": "password", "label": "Password", "type": "password"}
                    ]
                    set_required_fields(session, generic_fields)
                    discovered_fields = generic_fields
                
                session['missing_fields'] = discovered_fields
                persist_session(new_session_id, session)
                
                # Build natural message
                field_names = [f.get('field') for f in discovered_fields]
                field_hint = f" I'll need: {', '.join(field_names)}." if field_names else ""
                
                message = f"I need your login credentials for this portal.{field_hint} You can provide them naturally, like: 'username is john, password is secret123'"
                
                return jsonify({
                    'type': 'text',
                    'message': message,
                    'session_id': new_session_id
                }), 200
        
        # STEP 4: If portal NOT in saved_nodes - start credential-first flow
        if not existing_portal:
            node_type = intent_result.get('node_type')
            provided_credentials = intent_result.get('provided_credentials', {})
            
            if node_type == 'browser' and portal_url:
                # FIRST RUN: Credential-first execution
                session_id = str(uuid.uuid4())
                
                # Encrypt sensitive credentials
                encrypted_creds = {}
                if provided_credentials:
                    for key, value in provided_credentials.items():
                        if any(s in key.lower() for s in ['password', 'pin', 'secret', 'otp', 'token']):
                            encrypted_creds[key] = cipher.encrypt(str(value).encode()).decode()
                        else:
                            encrypted_creds[key] = value
                
                sessions[session_id] = {
                    "portal_name": portal_name,
                    "portal_url": portal_url,
                    "original_task": user_message,
                    "credentials": encrypted_creds,
                    "mode": "collecting_credentials" if not provided_credentials else "ready_to_run",
                    "status": "running" if provided_credentials else "waiting_input",
                    "history": [],
                    "streaming_url": None,
                    "node_type": "browser",
                    "last_fields_requested": [],
                    "retry_count": 0,
                    "busy": False,
                    "last_input_hash": "",
                    "last_input_time": 0,
                    "created_at": datetime.now().isoformat(),
                    "updated_at": datetime.now().isoformat()
                }
                
                log_session_event(session_id, f"First run for {portal_name}", {
                    "mode": sessions[session_id]["mode"],
                    "has_credentials": bool(provided_credentials)
                })
                
                # Persist session
                persist_session(session_id, sessions[session_id])
                
                # If we have credentials, execute immediately
                if provided_credentials:
                    result = run_orchestration_loop(session_id, user_message)
                    return jsonify(result), 200
                else:
                    # No credentials provided - ask naturally via chat
                    # Try lightweight discovery to know what fields might be needed
                    from utils import build_discovery_goal
                    discovery_goal = build_discovery_goal(portal_url)
                    
                    discovered_fields = []
                    try:
                        discovery_result = run_tinyfish(portal_url, discovery_goal, {}, session_id)
                        parsed = parse_tinyfish_result(discovery_result)
                        
                        if parsed.get("status") == "complete":
                            fields = parsed.get("data", {}).get("fields", [])
                            if fields:
                                normalized_fields = normalize_fields(fields)
                                discovered_fields = [f.get("field") for f in normalized_fields]
                                sessions[session_id]["last_fields_requested"] = discovered_fields
                                
                                log_session_event(session_id, f"Discovered {len(normalized_fields)} fields: {discovered_fields}")
                    except Exception as e:
                        log_session_event(session_id, f"Discovery failed: {e}")
                    
                    # Set generic fields if discovery didn't work
                    if not discovered_fields:
                        sessions[session_id]["last_fields_requested"] = ["username", "password"]
                    
                    # Return natural text message instead of form
                    field_hints = ""
                    if discovered_fields:
                        field_hints = f" I'll need: {', '.join(discovered_fields)}."
                    
                    return jsonify({
                        "type": "text",
                        "session_id": session_id,
                        "message": f"I need your login credentials for this portal.{field_hints} You can provide them naturally, like: 'username is john, password is secret123'"
                    }), 200
                
            elif node_type == 'api':
                # FALLBACK: Old flow for API portals
                job_id = str(uuid.uuid4())
                fields = [{"field": "api_key", "label": "API Key", "type": "password"}]
                
                jobs[job_id] = {
                    'intent': intent_result.get('intent'),
                    'portal_name': portal_name,
                    'portal_url': portal_url,
                    'node_type': node_type,
                    'instruction': intent_result.get('api_action'),
                    'status': 'waiting_auth',
                    'requires_confirmation': intent_result.get('requires_confirmation', False),
                    'confirmation_summary': intent_result.get('confirmation_summary', '')
                }
                
                print(f"[API Chat] No credentials for {portal_name} - requesting auth (job {job_id})")
                
                return jsonify({
                    'type': 'auth_request',
                    'job_id': job_id,
                    'portal_name': portal_name,
                    'portal_url': portal_url,
                    'node_type': node_type,
                    'needs_url': True,
                    'fields': fields,
                    'message': f"I need your API key to proceed."
                }), 200
        
        # STEP 5: Portal exists - check if confirmation needed
        if intent_result.get('requires_confirmation', False):
            job_id = str(uuid.uuid4())
            jobs[job_id] = {
                'intent': intent_result.get('intent'),
                'portal_id': existing_portal,
                'instruction': intent_result.get('tinyfish_instruction') or intent_result.get('api_action'),
                'status': 'waiting_confirm',
                'node_type': intent_result.get('node_type')
            }
            
            print(f"[API Chat] Action requires confirmation (job {job_id})")
            
            return jsonify({
                'type': 'confirmation_request',
                'job_id': job_id,
                'summary': intent_result.get('confirmation_summary')
            }), 200
        
        # STEP 6: Execute with merged credentials (saved + provided)
        print(f"[API Chat] Repeat run for {portal_name}")
        
        node_data = saved_nodes[existing_portal]
        
        if node_data.get('type') == 'browser':
            # REPEAT RUN: Merge saved credentials with provided credentials
            session_id = str(uuid.uuid4())
            
            # Get saved credentials (already encrypted)
            saved_creds = node_data.get('credentials', {}) or {}
            
            # Get provided credentials from current message
            provided_creds = intent_result.get('provided_credentials', {}) or {}
            
            # Merge credentials (provided overrides saved)
            merged_creds = merge_credentials(saved_creds, provided_creds, cipher)
            
            # Count usable credentials
            saved_count = len([k for k, v in saved_creds.items() if v])
            provided_count = len(provided_creds)
            merged_count = len([k for k, v in merged_creds.items() if v])
            
            print(f"[Repeat Run] Saved credentials: {saved_count}, Provided: {provided_count}, Merged: {merged_count}")
            print(f"[Repeat Run] Credential keys: {list(mask_credentials(merged_creds).keys())}")
            
            # Determine mode based on merged credentials
            if merged_count == 0:
                # No credentials at all - need to collect
                mode = "collecting_credentials"
                status = "waiting_input"
                print(f"[Repeat Run] No usable credentials, requesting input")
            else:
                # Have some credentials - try execution
                mode = "ready_to_run"
                status = "running"
                print(f"[Repeat Run] Using {merged_count} merged credentials")
            
            sessions[session_id] = {
                "portal_name": portal_name,
                "portal_url": node_data.get('portal_url'),
                "original_task": user_message,
                "credentials": merged_creds,
                "mode": mode,
                "status": status,
                "history": [],
                "streaming_url": None,
                "node_type": "browser",
                "last_fields_requested": [],
                "retry_count": 0,
                "busy": False,
                "last_input_hash": "",
                "last_input_time": 0,
                "created_at": datetime.now().isoformat(),
                "updated_at": datetime.now().isoformat()
            }
            
            log_session_event(session_id, f"Repeat run for {portal_name}", {
                "mode": mode,
                "saved_creds": saved_count,
                "provided_creds": provided_count,
                "merged_creds": merged_count
            })
            
            # Persist session
            persist_session(session_id, sessions[session_id])
            
            # If no credentials, ask for them naturally via chat
            if mode == "collecting_credentials":
                # Try discovery to know what fields might be needed
                from utils import build_discovery_goal
                discovery_goal = build_discovery_goal(node_data.get('portal_url'))
                
                discovered_fields = []
                try:
                    discovery_result = run_tinyfish(node_data.get('portal_url'), discovery_goal, {}, session_id)
                    parsed = parse_tinyfish_result(discovery_result)
                    
                    if parsed.get("status") == "complete":
                        fields = parsed.get("data", {}).get("fields", [])
                        if fields:
                            normalized_fields = normalize_fields(fields)
                            discovered_fields = [f.get("field") for f in normalized_fields]
                            sessions[session_id]["last_fields_requested"] = discovered_fields
                except Exception as e:
                    log_session_event(session_id, f"Discovery failed: {e}")
                
                # Set generic fields if discovery didn't work
                if not discovered_fields:
                    sessions[session_id]["last_fields_requested"] = ["username", "password"]
                
                # Return natural text message instead of form
                field_hints = ""
                if discovered_fields:
                    field_hints = f" I'll need: {', '.join(discovered_fields)}."
                
                return jsonify({
                    "type": "text",
                    "session_id": session_id,
                    "message": f"I need your credentials to access this portal.{field_hints} You can provide them naturally, like: 'username is john, password is secret123'"
                }), 200
            
            # Execute with merged credentials
            result = run_orchestration_loop(session_id, user_message)
            
            return jsonify(result), 200
        
        elif node_data.get('type') == 'api':
            # TODO: Implement API calls
            return jsonify({
                'type': 'text',
                'message': f"API execution not yet implemented. Action: {intent_result.get('api_action')}"
            }), 200
        
    except Exception as e:
        print(f"[API Chat] Error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@app.route('/api/session/input', methods=['POST'])
def session_input():
    """Receives user input during orchestration loop and continues execution"""
    try:
        data = request.json or {}
        
        # Debug logging with safe masking
        log_session_event("API", "Received /api/session/input request")
        
        session_id = data.get('session_id')
        
        # Support both payload shapes:
        # 1. {"session_id": "...", "input": {...}}
        # 2. {"session_id": "...", "field1": "value1", "field2": "value2"}
        user_input = data.get('input')
        if not user_input:
            user_input = {k: v for k, v in data.items() if k != 'session_id'}
        
        # Validation
        if not session_id:
            return jsonify({'error': 'Missing session_id'}), 400
        
        if not user_input:
            return jsonify({'error': 'Missing input data'}), 400
        
        if session_id not in sessions:
            return jsonify({'error': 'Session not found'}), 404
        
        session = sessions[session_id]
        
        # Check if session is busy (prevent concurrent execution)
        if session.get("busy", False):
            log_session_event(session_id, "Session busy, rejecting duplicate request")
            return jsonify({
                'type': 'text',
                'message': 'Navi is already processing that step.'
            }), 200
        
        # Deduplication: check if this is a duplicate submission
        input_hash = compute_input_hash(user_input)
        last_hash = session.get("last_input_hash", "")
        last_time = session.get("last_input_time", 0)
        current_time = time.time()
        
        if input_hash == last_hash and (current_time - last_time) < 5:
            log_session_event(session_id, "Duplicate input detected, ignoring")
            return jsonify({
                'type': 'text',
                'message': 'Processing your previous request...'
            }), 200
        
        # Update deduplication tracking
        session["last_input_hash"] = input_hash
        session["last_input_time"] = current_time
        
        # Set busy flag
        session["busy"] = True
        
        try:
            log_session_event(session_id, "Processing input", {"fields": list(mask_credentials(user_input).keys())})
            
            # Add new credentials to session
            for key, value in user_input.items():
                # Encrypt sensitive fields
                if any(sensitive in key.lower() for sensitive in ['password', 'pin', 'secret', 'otp', 'token']):
                    session["credentials"][key] = cipher.encrypt(str(value).encode()).decode()
                    log_session_event(session_id, f"Encrypted field: {key}")
                else:
                    session["credentials"][key] = value
                    log_session_event(session_id, f"Stored field: {key}")
            
            # Update session timestamp
            session["updated_at"] = datetime.now().isoformat()
            
            # Recalculate credential readiness
            is_ready, suggested_mode = determine_credential_readiness(session)
            
            if is_ready:
                # We have enough credentials - execute
                session["mode"] = "ready_to_run"
                session["status"] = "running"
                
                log_session_event(session_id, "Credentials ready, executing")
                
                # Persist session state
                persist_session(session_id, session)
                
                # Execute orchestration loop
                original_task = session.get("original_task")
                result = run_orchestration_loop(session_id, original_task)
                
                return jsonify(result), 200
            else:
                # Still not enough credentials
                session["mode"] = "collecting_credentials"
                session["status"] = "waiting_input"
                
                log_session_event(session_id, "Still collecting credentials")
                
                # Persist session state
                persist_session(session_id, session)
                
                return jsonify({
                    "type": "text",
                    "message": "I need more information to proceed. Please provide additional credentials.",
                    "session_id": session_id
                }), 200
            
        finally:
            # Always release the lock
            session["busy"] = False
        
    except Exception as e:
        log_session_event("API", f"Error in /api/session/input: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({
            'type': 'text',
            'message': 'I encountered an error processing your input. Please try again.'
        }), 500

@app.route('/api/debug/reset', methods=['POST'])
def debug_reset():
    """
    DEBUG ROUTE - Local development only
    Clears all Navi state for clean-room testing
    WARNING: This route must NOT exist in production
    """
    try:
        # Clear in-memory state
        saved_nodes.clear()
        sessions.clear()
        jobs.clear()
        
        # Clear SQLite state
        result = reset_all_state()
        
        print("[DEBUG RESET] All Navi state cleared")
        
        return jsonify({
            'success': True,
            'message': 'All Navi state cleared.',
            'nodes_deleted': result['nodes_deleted'],
            'sessions_deleted': result['sessions_deleted']
        }), 200
        
    except Exception as e:
        print(f"[DEBUG RESET] Error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.route('/api/agent/resume_and_save', methods=['POST'])
def resume_and_save():
    """LEGACY: Receives credentials, saves node, executes TinyFish, and returns result"""
    try:
        data = request.json
        job_id = data.get('job_id', '')
        credentials = data.get('credentials', {})
        portal_url_from_request = data.get('portal_url', '')
        
        if not job_id or not credentials:
            return jsonify({'error': 'Missing job_id or credentials'}), 400
        
        # Look up the job
        if job_id not in jobs:
            return jsonify({'error': 'Job not found'}), 404
        
        job = jobs[job_id]
        portal_name = job.get('portal_name')
        portal_url = portal_url_from_request or job.get('portal_url')
        node_type = job.get('node_type')
        instruction = job.get('instruction')
        
        print(f"[Resume & Save] Job {job_id}: {portal_name}")
        print(f"[Resume & Save] Portal URL: {portal_url}")
        print(f"[Resume & Save] Credentials received: {list(credentials.keys())}")
        
        # Generate unique portal ID
        portal_id = str(uuid.uuid4())
        
        # Encrypt credentials
        encrypted_creds = {}
        for key, value in credentials.items():
            encrypted_creds[key] = cipher.encrypt(value.encode()).decode()
        
        # Save node to saved_nodes
        saved_nodes[portal_id] = {
            'type': node_type,
            'portal_name': portal_name,
            'portal_url': portal_url,
            'credentials': encrypted_creds
        }
        
        print(f"[Resume & Save] Saved node {portal_id} for {portal_name}")
        
        # Update job status
        jobs[job_id]['status'] = 'running'
        jobs[job_id]['portal_id'] = portal_id
        
        # Execute TinyFish or API call
        result_message = ""
        
        if node_type == 'browser':
            print(f"[Resume & Save] Executing TinyFish for {portal_name}")
            
            # Phase 2: Build execute instruction with all placeholders replaced
            filled_instruction = instruction
            
            print(f"[Resume & Save] Original instruction: {instruction}")
            
            # Add portal_url to credentials so it can be replaced
            credentials['portal_url'] = portal_url
            
            # Replace all credential placeholders dynamically
            for key, value in credentials.items():
                placeholder = f"{{{key}}}"
                filled_instruction = filled_instruction.replace(placeholder, str(value))
            
            print(f"[Resume & Save] Filled instruction: {filled_instruction}")
            
            # Execute with portal_url as first argument (credentials already in instruction)
            # Pass job_id so streaming_url is saved immediately when event is received
            tinyfish_response = run_tinyfish(portal_url, filled_instruction, {}, job_id)
            
            # Streaming URL already saved to job inside run_tinyfish when event received
            streaming_url = tinyfish_response.get('streaming_url')
            
            result_data = tinyfish_response.get('result', {})
            result_message = f"✅ Successfully connected to {portal_name}!\n\nResult: {json.dumps(result_data, indent=2)}"
        elif node_type == 'api':
            streaming_url = None
            result_message = f"✅ Successfully connected to {portal_name}! API execution will be implemented soon."
        
        # Mark job as done
        jobs[job_id]['status'] = 'done'
        
        # Return result with node info for canvas and streaming URL
        return jsonify({
            'type': 'text',
            'message': result_message,
            'streaming_url': streaming_url,
            'node': {
                'id': portal_id,
                'portal_name': portal_name,
                'node_type': node_type,
                'portal_url': portal_url
            }
        }), 200
        
    except Exception as e:
        print(f"[Resume & Save] Error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@app.route('/api/agent/stream-status/<job_id>', methods=['GET'])
def stream_status(job_id):
    """Returns streaming URL status for a job (for polling)"""
    if job_id not in jobs:
        return jsonify({'error': 'Job not found'}), 404
    
    job = jobs[job_id]
    return jsonify({
        'status': job.get('status'),
        'streaming_url': job.get('streaming_url')
    }), 200

@app.route('/api/agent/confirm', methods=['POST'])
def confirm_action():
    """Handles user confirmation after confirmation_request"""
    try:
        data = request.json
        job_id = data.get('job_id', '')
        confirmed = data.get('confirmed', False)
        
        if not job_id:
            return jsonify({'error': 'Missing job_id'}), 400
        
        if job_id not in jobs:
            return jsonify({'error': 'Job not found'}), 404
        
        job = jobs[job_id]
        
        # If user cancelled
        if not confirmed:
            print(f"[Confirm] User cancelled job {job_id}")
            del jobs[job_id]
            return jsonify({
                'type': 'text',
                'message': "Okay, I've cancelled that action."
            }), 200
        
        # User confirmed - execute the action
        print(f"[Confirm] User confirmed job {job_id}")
        
        portal_id = job.get('portal_id')
        instruction = job.get('instruction')
        node_type = job.get('node_type')
        
        if portal_id not in saved_nodes:
            return jsonify({'error': 'Portal not found'}), 404
        
        node_data = saved_nodes[portal_id]
        
        # Execute based on node type
        if node_type == 'browser':
            # Decrypt credentials
            decrypted_creds = {}
            for key, encrypted_value in node_data.get('credentials', {}).items():
                decrypted_creds[key] = cipher.decrypt(encrypted_value.encode()).decode()
            
            # Run TinyFish
            result = run_tinyfish(instruction, decrypted_creds)
            
            # Mark job as done
            jobs[job_id]['status'] = 'done'
            
            return jsonify({
                'type': 'text',
                'message': f"✅ Action completed!\n\nResult: {json.dumps(result, indent=2)}"
            }), 200
        
        elif node_type == 'api':
            # TODO: Implement API calls
            jobs[job_id]['status'] = 'done'
            return jsonify({
                'type': 'text',
                'message': "API execution not yet implemented."
            }), 200
        
    except Exception as e:
        print(f"[Confirm] Error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@app.route('/api/nodes', methods=['GET', 'POST'])
def nodes_endpoint():
    """Handle both GET (list nodes) and POST (create node) for /api/nodes"""
    
    # POST: Create a new portal node manually
    if request.method == 'POST':
        try:
            print("[Add Portal] POST request received")
            data = request.json
            portal_name = data.get('portal_name', '')
            portal_url = data.get('portal_url', '')
            credentials = data.get('credentials', {})
            node_type = data.get('node_type', 'browser')
            
            if not portal_name or not portal_url:
                print("[Add Portal] Error: Missing portal_name or portal_url")
                return jsonify({'error': 'portal_name and portal_url are required'}), 400
            
            print(f"[Add Portal] Creating portal: {portal_name}")
            print(f"[Add Portal] URL: {portal_url}")
            print(f"[Add Portal] Type: {node_type}")
            print(f"[Add Portal] Credentials: {list(credentials.keys())}")
            
            # Generate unique portal ID
            portal_id = str(uuid.uuid4())
            
            # Compute portal key
            from utils import normalize_portal_key
            portal_key = normalize_portal_key(portal_name, portal_url)
            
            # Encrypt credentials
            encrypted_creds = {}
            for key, value in credentials.items():
                if value:
                    try:
                        encrypted_creds[key] = cipher.encrypt(str(value).encode()).decode()
                    except Exception as e:
                        print(f"[Add Portal] Error encrypting {key}: {e}")
                        encrypted_creds[key] = value
            
            # Save node to saved_nodes
            saved_nodes[portal_id] = {
                'type': node_type,
                'portal_name': portal_name,
                'portal_url': portal_url,
                'portal_key': portal_key,
                'credentials': encrypted_creds
            }
            
            # Persist to database
            print("[Add Portal] Persisting node to database...")
            persist_node(portal_id, saved_nodes[portal_id])
            print("[Add Portal] Node persisted successfully")
            
            # Create edge from navi_agent to this portal (auto-connect)
            edge_id = f"edge-navi_agent-{portal_id}"
            edges_storage[edge_id] = {
                'source': 'navi_agent',
                'target': portal_id
            }
            
            print(f"[Add Portal] Node created: {portal_id[:8]} for {portal_name}")
            print(f"[Add Portal] Edge created: navi_agent -> {portal_id[:8]}")
            
            return jsonify({
                'success': True,
                'node_id': portal_id,
                'portal_name': portal_name,
                'message': f'Portal {portal_name} created successfully'
            }), 200
            
        except Exception as e:
            print(f"[Add Portal] Error: {e}")
            import traceback
            traceback.print_exc()
            return jsonify({'error': str(e)}), 500
    
    # GET: Return all nodes and edges
    else:  # request.method == 'GET'
        try:
            nodes = []
            edges = []
            
            # Central Navi core node (use navi_agent as ID for connection gating)
            navi_core = {
                'id': 'navi_agent',
                'type': 'naviCore',
                'position': {'x': 400, 'y': 300},
                'data': {'label': 'Navi', 'status': 'active'}
            }
            nodes.append(navi_core)
            
            # Position portal nodes in a circle around Navi core
            num_portals = len(saved_nodes)
            radius = 250
            
            for idx, (portal_id, node_data) in enumerate(saved_nodes.items()):
                # Calculate position in circle
                angle = (2 * math.pi * idx) / max(num_portals, 1)
                x = 400 + radius * math.cos(angle)
                y = 300 + radius * math.sin(angle)
                
                # Check if node is connected to navi_agent via edges
                is_connected = False
                for edge_id, edge_data in edges_storage.items():
                    if edge_data.get('source') == 'navi_agent' and edge_data.get('target') == portal_id:
                        is_connected = True
                        break
                
                # Compute node status
                credentials = node_data.get('credentials', {})
                has_credentials = credentials and len(credentials) > 0
                
                if is_connected and has_credentials:
                    node_status = 'connected'
                elif is_connected:
                    node_status = 'connected_no_creds'
                else:
                    node_status = 'not_connected'
                
                # Create portal node
                portal_node = {
                    'id': portal_id,
                    'type': 'universalPortal',
                    'position': {'x': x, 'y': y},
                    'data': {
                        'portalName': node_data.get('portal_name'),
                        'portalUrl': node_data.get('portal_url'),
                        'portalKey': node_data.get('portal_key'),
                        'nodeType': node_data.get('type'),
                        'status': node_status,
                        'isConnected': is_connected
                    }
                }
                nodes.append(portal_node)
                
                # Create edge from Navi core to portal (only if connected)
                if is_connected:
                    edge = {
                        'id': f'edge-navi-{portal_id}',
                        'source': 'navi_agent',
                        'target': portal_id,
                        'animated': True,
                        'style': {'stroke': '#8b5cf6', 'strokeWidth': 2}
                    }
                    edges.append(edge)
            
            return jsonify({
                'nodes': nodes,
                'edges': edges
            }), 200
            
        except Exception as e:
            print(f"[Get Nodes] Error: {e}")
            import traceback
            traceback.print_exc()
            return jsonify({'error': str(e)}), 500

@app.route('/api/nodes/<node_id>', methods=['DELETE'])
def delete_node_endpoint(node_id):
    """Delete a saved node and its credentials"""
    try:
        print(f"[Node Delete] Deleting node {node_id}")
        
        # Check if node exists
        if node_id not in saved_nodes:
            return jsonify({
                'success': False,
                'error': 'Node not found'
            }), 404
        
        # Remove from in-memory state
        del saved_nodes[node_id]
        
        # Remove associated edges
        edges_to_remove = [edge_id for edge_id, edge_data in edges_storage.items() 
                          if edge_data['target'] == node_id or edge_data['source'] == node_id]
        for edge_id in edges_to_remove:
            del edges_storage[edge_id]
        
        # Remove from database
        from db_helpers import delete_node
        delete_node(node_id)
        
        print(f"[Node Delete] Deleted node {node_id}")
        
        return jsonify({
            'success': True,
            'node_id': node_id,
            'message': 'Node deleted successfully.'
        }), 200
        
    except Exception as e:
        print(f"[Node Delete] Error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.route('/api/session/<session_id>/status', methods=['GET'])
def get_session_status(session_id):
    """Get live session status for frontend polling during execution"""
    try:
        if session_id not in sessions:
            return jsonify({'error': 'Session not found'}), 404
        
        session = sessions[session_id]
        
        # Build status response
        status_response = {
            'session_id': session_id,
            'mode': session.get('mode'),
            'status': session.get('status'),
            'live_status': session.get('live_status', 'idle'),
            'live_message': session.get('live_message'),
            'streaming_url': session.get('streaming_url'),
            'complete': session.get('mode') == 'complete',
            'error': session.get('mode') == 'error' or session.get('live_status') in ['error', 'timeout'],
            'has_result': session.get('last_result') is not None,
            'last_result_summary': session.get('last_result_summary'),
            'final_response_message': session.get('final_response_message'),
            'has_final_response': session.get('has_final_response', False),
            'updated_at': session.get('updated_at')
        }
        
        return jsonify(status_response), 200
        
    except Exception as e:
        print(f"[Session Status] Error: {e}")
        return jsonify({'error': str(e)}), 500

# Health check endpoint for deployment monitoring
@app.route('/health', methods=['GET'])
def health_check():
    """Simple health check endpoint for deployment platforms"""
    return jsonify({'ok': True}), 200

print("[Startup] All routes registered successfully")
print("[Startup] Backend initialization complete - ready for requests")

if __name__ == '__main__':
    # Read port from environment for deployment compatibility
    port = int(os.environ.get('PORT', 5000))
    print(f"[Startup] Running in __main__ mode on port {port}")
    # Bind to 0.0.0.0 to allow external connections (required for Render/Railway)
    app.run(host='0.0.0.0', debug=True, port=port)