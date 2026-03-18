"""
Intent and Credential Extractors for Navi
Uses Gemini to extract structured information from natural language
"""

import json
from google import genai
from google.genai import types

# Gemini model configuration
GEMINI_MODEL = 'gemini-2.5-flash'


def extract_task_intent(user_message, saved_nodes, client):
    """
    Extract task intent from user message.
    
    Returns:
        {
            "intent": str,
            "portal_name": str,
            "portal_url": str | None,
            "node_type": "browser" | None,
            "provided_credentials": dict | None,
            "message": str
        }
    """
    
    # Create summary of saved nodes for context
    saved_nodes_summary = {}
    for portal_id, node_data in saved_nodes.items():
        saved_nodes_summary[portal_id] = {
            "portal_name": node_data.get("portal_name"),
            "type": node_data.get("type"),
            "portal_url": node_data.get("portal_url")
        }
    
    system_prompt = f"""You are Navi, an AI work assistant that can operate any website or portal on behalf of the user.

You have access to the user's saved nodes (connected portals):
{json.dumps(saved_nodes_summary, indent=2)}

Your job is to extract structured intent from the user's message.

Extract:
1. intent: What the user wants to do
2. portal_name: Name of the portal/website
3. portal_url: Full URL if mentioned (null if not mentioned)
4. node_type: "browser" for web portals, null for general conversation
5. provided_credentials: Any login credentials mentioned in the message (null if none)
6. message: A conversational response if this is general chat

For provided_credentials, extract any authentication details mentioned:
- Venue/Venue ID/input_venue
- Username/User ID/LoginId/email
- Password/pass
- OTP/code
- Any other credential fields

Return ONLY valid JSON in this exact format:
{{
  "intent": "fetch schedule from ABI portal",
  "portal_name": "ABI",
  "portal_url": "https://ess.abimm.com/ABIMM_ASP/Request.aspx",
  "node_type": "browser",
  "provided_credentials": {{
    "input_venue": "Canucks",
    "LoginId": "Tiwari8703",
    "password": "750621"
  }},
  "message": null
}}

For general conversation (no task):
{{
  "intent": null,
  "portal_name": null,
  "portal_url": null,
  "node_type": null,
  "provided_credentials": null,
  "message": "I'm here to help you access portals and fetch data. What would you like me to do?"
}}

Examples:

User: "Fetch my ABI schedule. Venue is Canucks, User ID is Tiwari8703, Password is 750621."
Output: {{"intent": "fetch schedule", "portal_name": "ABI", "portal_url": null, "node_type": "browser", "provided_credentials": {{"input_venue": "Canucks", "LoginId": "Tiwari8703", "password": "750621"}}, "message": null}}

User: "Check my schedule from https://ess.abimm.com"
Output: {{"intent": "check schedule", "portal_name": "ESS Portal", "portal_url": "https://ess.abimm.com", "node_type": "browser", "provided_credentials": null, "message": null}}

User: "How are you?"
Output: {{"intent": null, "portal_name": null, "portal_url": null, "node_type": null, "provided_credentials": null, "message": "I'm doing well! I can help you access portals and fetch data. What would you like me to do?"}}
"""
    
    try:
        print(f"[Extractor] Calling Gemini for task intent extraction")
        
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=f"{system_prompt}\n\nUser message: \"{user_message}\"",
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                max_output_tokens=1000,
                temperature=0.3
            )
        )
        
        result = json.loads(response.text)
        
        # Log extracted intent
        if result.get('node_type'):
            creds_count = len(result.get('provided_credentials', {}) or {})
            print(f"[Extractor] Intent: {result.get('intent')}, Portal: {result.get('portal_name')}, Credentials: {creds_count}")
        else:
            print(f"[Extractor] General chat detected")
        
        return result
        
    except Exception as e:
        print(f"[Extractor] Error extracting task intent: {e}")
        import traceback
        traceback.print_exc()
        
        # Fallback
        return {
            "intent": None,
            "portal_name": None,
            "portal_url": None,
            "node_type": None,
            "provided_credentials": None,
            "message": "I encountered an error understanding your request. Could you rephrase it?"
        }


def extract_session_input(user_message, required_fields, client):
    """
    Extract credentials from user message based on required fields.
    Field-driven extraction - maps natural language to exact field names.
    
    Args:
        user_message: Natural language message from user
        required_fields: List of field objects like:
            [
                {"field": "input_venue", "label": "Venue ID", "type": "text"},
                {"field": "LoginId", "label": "User ID", "type": "text"},
                {"field": "password", "label": "Password", "type": "password"}
            ]
        client: Gemini client
    
    Returns:
        {
            "credentials": {
                "field_name": "value"
            }
        }
    """
    
    if not required_fields:
        # No required fields specified - use generic extraction
        required_fields = [
            {"field": "username", "label": "Username", "type": "text"},
            {"field": "password", "label": "Password", "type": "password"}
        ]
    
    # Build field mapping for Gemini
    field_descriptions = []
    for field in required_fields:
        field_name = field.get('field')
        field_label = field.get('label', field_name)
        field_type = field.get('type', 'text')
        field_descriptions.append(f"- {field_name}: {field_label} ({field_type})")
    
    fields_text = "\n".join(field_descriptions)
    
    extraction_prompt = f"""Extract credentials from this user message and map them to the exact field names specified.

User message: "{user_message}"

Required fields:
{fields_text}

Map the user's natural language to these EXACT field names. Do not invent new field names.

Support various formats:
- "Venue: Canucks" or "Venue is Canucks" → input_venue
- "username - akash" or "user: akash" → LoginId or username (depending on field list)
- "pass=123" or "password 123" → password
- "OTP 483921" or "code: 483921" → otp
- "User ID is Tiwari8703" → LoginId

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

Required fields: input_venue, LoginId, password
User: "Venue is Canucks, user ID is Tiwari8703, password is 750621"
Output: {{"credentials": {{"input_venue": "Canucks", "LoginId": "Tiwari8703", "password": "750621"}}}}

Required fields: username, password
User: "username: john@example.com, pass: secret123"
Output: {{"credentials": {{"username": "john@example.com", "password": "secret123"}}}}

Required fields: otp
User: "My OTP is 483921"
Output: {{"credentials": {{"otp": "483921"}}}}

IMPORTANT: Only return fields that are in the required fields list. Do not guess or invent field names.
"""
    
    try:
        print(f"[Extractor] Extracting session input for fields: {[f.get('field') for f in required_fields]}")
        
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=extraction_prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                max_output_tokens=500,
                temperature=0.3
            )
        )
        
        result = json.loads(response.text)
        credentials = result.get('credentials', {})
        
        print(f"[Extractor] Session input extracted: {list(credentials.keys())}")
        
        return credentials
        
    except Exception as e:
        print(f"[Extractor] Error extracting session input: {e}")
        import traceback
        traceback.print_exc()
        return {}
