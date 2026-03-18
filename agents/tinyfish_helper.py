import os
import json
import requests
from dotenv import load_dotenv

load_dotenv()

TINYFISH_API_KEY = os.getenv("TINYFISH_API_KEY", "")
TINYFISH_URL = "https://agent.tinyfish.ai/v1/automation/run-sse"


def extract_from_html(html: str, goal: str, max_steps: int = 10) -> dict:
    """
    Calls TinyFish automation API to extract data from provided HTML.
    This is used when the Chrome extension has already authenticated and captured the page HTML.
    Returns parsed result as dict.
    """
    headers = {
        "X-API-Key": TINYFISH_API_KEY,
        "Content-Type": "application/json",
        "Accept": "text/event-stream"
    }

    # TinyFish can process HTML directly without navigating
    payload = {
        "html": html,
        "goal": goal,
        "maxSteps": max_steps
    }

    print(f"[TinyFish HTML] POST {TINYFISH_URL}")
    print(f"[TinyFish HTML] Goal: {goal[:200]}...")
    print(f"[TinyFish HTML] HTML length: {len(html)}")

    response = requests.post(TINYFISH_URL, json=payload, headers=headers, stream=True, timeout=300)
    print(f"[TinyFish HTML] HTTP {response.status_code}")
    response.raise_for_status()

    result_text = ""
    all_events = []
    last_data = ""

    for line in response.iter_lines(decode_unicode=True):
        if not line:
            continue

        print(f"[TinyFish HTML] RAW LINE: {line[:300]}")

        if line.startswith("data: "):
            data_str = line[6:]
            try:
                event = json.loads(data_str)
                event_type = event.get("type", "unknown")
                all_events.append(event_type)
                print(f"[TinyFish HTML] Event type={event_type}, keys={list(event.keys())}")

                # Capture data from any event that has it
                for key in ("data", "result", "output", "content", "text", "message", "resultJson"):
                    val = event.get(key)
                    if val:
                        print(f"[TinyFish HTML]   {key} = {str(val)[:500]}")
                        last_data = val

                if event_type == "result":
                    result_text = event.get("data", "") or event.get("result", "") or event.get("resultJson", "")
                elif event_type in ("done", "complete", "completed", "finished", "final", "COMPLETED", "DONE"):
                    result_text = event.get("resultJson", "") or event.get("data", "") or event.get("result", "") or event.get("output", "")
                    print(f"[TinyFish HTML] FINAL EVENT: type={event_type}, resultJson={str(event.get('resultJson', ''))[:500]}")
                elif event_type == "error":
                    err = event.get("data", "") or event.get("error", "Unknown error")
                    print(f"[TinyFish HTML] ERROR: {err}")
                    raise RuntimeError(f"TinyFish error: {err}")
            except json.JSONDecodeError:
                print(f"[TinyFish HTML] Non-JSON data: {data_str[:300]}")
                last_data = data_str
        elif line.startswith("event:"):
            print(f"[TinyFish HTML] SSE event header: {line}")

    print(f"[TinyFish HTML] Stream ended. Events seen: {all_events}")
    print(f"[TinyFish HTML] result_text length: {len(str(result_text))}")
    print(f"[TinyFish HTML] last_data length: {len(str(last_data))}")

    # Use result_text if we got one, otherwise fall back to last_data
    final = result_text if result_text else last_data

    if isinstance(final, dict):
        return final

    if isinstance(final, str) and final.strip():
        try:
            return json.loads(final)
        except (json.JSONDecodeError, TypeError):
            return {"raw": final}

    return {"raw": str(final), "_events": all_events}


def run_agent(url: str, goal: str, max_steps: int = 10) -> dict:
    """
    Calls TinyFish automation API to run a browser agent.
    Returns parsed result as dict.
    """
    headers = {
        "X-API-Key": TINYFISH_API_KEY,
        "Content-Type": "application/json",
        "Accept": "text/event-stream"
    }

    payload = {
        "url": url,
        "goal": goal,
        "maxSteps": max_steps
    }

    print(f"[TinyFish] POST {TINYFISH_URL}")
    print(f"[TinyFish] Goal: {goal[:200]}...")
    print(f"[TinyFish] API Key: {TINYFISH_API_KEY[:10]}...")

    response = requests.post(TINYFISH_URL, json=payload, headers=headers, stream=True, timeout=300)
    print(f"[TinyFish] HTTP {response.status_code}")
    response.raise_for_status()

    result_text = ""
    all_events = []
    last_data = ""

    for line in response.iter_lines(decode_unicode=True):
        if not line:
            continue

        print(f"[TinyFish] RAW LINE: {line[:300]}")

        if line.startswith("data: "):
            data_str = line[6:]
            try:
                event = json.loads(data_str)
                event_type = event.get("type", "unknown")
                all_events.append(event_type)
                print(f"[TinyFish] Event type={event_type}, keys={list(event.keys())}")

                # Capture data from any event that has it
                for key in ("data", "result", "output", "content", "text", "message", "resultJson"):
                    val = event.get(key)
                    if val:
                        print(f"[TinyFish]   {key} = {str(val)[:500]}")
                        last_data = val

                if event_type == "result":
                    result_text = event.get("data", "") or event.get("result", "") or event.get("resultJson", "")
                elif event_type in ("done", "complete", "completed", "finished", "final", "COMPLETED", "DONE"):
                    result_text = event.get("resultJson", "") or event.get("data", "") or event.get("result", "") or event.get("output", "")
                    print(f"[TinyFish] FINAL EVENT: type={event_type}, resultJson={str(event.get('resultJson', ''))[:500]}")
                elif event_type == "error":
                    err = event.get("data", "") or event.get("error", "Unknown error")
                    print(f"[TinyFish] ERROR: {err}")
                    raise RuntimeError(f"TinyFish error: {err}")
            except json.JSONDecodeError:
                print(f"[TinyFish] Non-JSON data: {data_str[:300]}")
                last_data = data_str
        elif line.startswith("event:"):
            print(f"[TinyFish] SSE event header: {line}")

    print(f"[TinyFish] Stream ended. Events seen: {all_events}")
    print(f"[TinyFish] result_text length: {len(str(result_text))}")
    print(f"[TinyFish] last_data length: {len(str(last_data))}")

    # Use result_text if we got one, otherwise fall back to last_data
    final = result_text if result_text else last_data

    if isinstance(final, dict):
        return final

    if isinstance(final, str) and final.strip():
        try:
            return json.loads(final)
        except (json.JSONDecodeError, TypeError):
            return {"raw": final}

    return {"raw": str(final), "_events": all_events}
