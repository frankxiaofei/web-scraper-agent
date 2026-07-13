#!/usr/bin/env python3
"""
Direct WebSocket client for Kimi WebBridge daemon.
Bypasses the skill_execute API format issue.
"""
import asyncio
import json
import sys
import uuid
import websockets

DAEMON_URL = "ws://127.0.0.1:10086/ws"

async def send_command(command: dict, timeout: float = 30.0) -> dict:
    """Send a command to WebBridge daemon and wait for response."""
    headers = {"Origin": "http://localhost:10086"}
    async with websockets.connect(DAEMON_URL, max_size=10_485_760, additional_headers=headers) as ws:
        msg = json.dumps(command)
        print(f"[SEND] {msg}", file=sys.stderr)
        await ws.send(msg)
        
        # Wait for response
        try:
            resp = await asyncio.wait_for(ws.recv(), timeout=timeout)
            data = json.loads(resp)
            print(f"[RECV] {json.dumps(data, ensure_ascii=False)[:500]}", file=sys.stderr)
            return data
        except asyncio.TimeoutError:
            print(f"[TIMEOUT] No response after {timeout}s", file=sys.stderr)
            return {"error": "timeout"}

async def main():
    if len(sys.argv) < 2:
        print("Usage: webbridge_direct.py <action> [args...]")
        print("Actions:")
        print("  navigate <url> [new_tab=true]")
        print("  snapshot [session_id]")
        print("  click <selector> [session_id]")
        print("  evaluate <js_code> [session_id]")
        print("  extract_list [selector] [session_id]")
        print("  close [session_id]")
        print("  list_sessions")
        sys.exit(1)
    
    action = sys.argv[1]
    cmd = {"id": str(uuid.uuid4()), "type": action}
    
    if action == "navigate":
        cmd["url"] = sys.argv[2]
        cmd["newTab"] = sys.argv[3].lower() == "true" if len(sys.argv) > 3 else True
    elif action == "snapshot":
        if len(sys.argv) > 2:
            cmd["sessionId"] = sys.argv[2]
    elif action == "click":
        cmd["selector"] = sys.argv[2]
        if len(sys.argv) > 3:
            cmd["sessionId"] = sys.argv[3]
    elif action == "evaluate":
        cmd["expression"] = sys.argv[2]
        if len(sys.argv) > 3:
            cmd["sessionId"] = sys.argv[3]
    elif action == "extract_list":
        if len(sys.argv) > 2:
            cmd["selector"] = sys.argv[2]
        if len(sys.argv) > 3:
            cmd["sessionId"] = sys.argv[3]
    elif action == "close":
        cmd["type"] = "closeSession"
        if len(sys.argv) > 2:
            cmd["sessionId"] = sys.argv[2]
    elif action == "list_sessions":
        cmd["type"] = "listSessions"
    
    result = await send_command(cmd)
    print(json.dumps(result, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    asyncio.run(main())
