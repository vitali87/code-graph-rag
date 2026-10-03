"""Tiny OpenAI-compatible chat-completions mock: replies with the text in reply.txt."""
import json, sys, time, os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
HERE = os.path.dirname(os.path.abspath(__file__))
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        body = json.dumps({"object": "list", "data": [{"id": "mock-model", "object": "model", "owned_by": "me"}]}).encode()
        self.send_response(200); self.send_header("content-type", "application/json"); self.send_header("content-length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def do_POST(self):
        n = int(self.headers.get("content-length", 0)); req = json.loads(self.rfile.read(n) or b"{}")
        with open(os.path.join(HERE, "requests.jsonl"), "a") as f: f.write(json.dumps(req) + "\n")
        reply = open(os.path.join(HERE, "reply.txt")).read()
        tools = req.get("tools") or []
        items = req.get("input") or []
        done = any(isinstance(i, dict) and i.get("type") == "function_call_output" for i in items)
        if self.path.rstrip("/").endswith("/responses") and tools and not done and os.path.exists(os.path.join(HERE, "toolcall.on")):
            tool = next((t for t in tools if "query" in t.get("name", "")), tools[0])
            params = tool.get("parameters", {})
            arg = (params.get("required") or list(params.get("properties", {})))[0]
            out = [{"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": tool["name"],
                    "arguments": json.dumps({arg: "which functions are defined?"}), "status": "completed"}]
            body = json.dumps({"id": "resp_2", "object": "response", "created_at": int(time.time()), "model": req.get("model"),
                "status": "completed", "parallel_tool_calls": False, "tool_choice": "auto", "tools": [], "output": out,
                "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2,
                          "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0}}}).encode()
            self.send_response(200); self.send_header("content-type", "application/json"); self.send_header("content-length", str(len(body))); self.end_headers(); self.wfile.write(body)
            return
        if not tools and os.path.exists(os.path.join(HERE, "cypher.txt")):
            reply = open(os.path.join(HERE, "cypher.txt")).read()
        if self.path.rstrip("/").endswith("/responses"):
            body = json.dumps({"id": "resp_1", "object": "response", "created_at": int(time.time()), "model": req.get("model", "mock-model"),
                "status": "completed", "parallel_tool_calls": False, "tool_choice": "auto", "tools": [],
                "output": [{"type": "message", "id": "msg_1", "status": "completed", "role": "assistant",
                            "content": [{"type": "output_text", "text": reply, "annotations": []}]}],
                "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2,
                          "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0}}}).encode()
            self.send_response(200); self.send_header("content-type", "application/json"); self.send_header("content-length", str(len(body))); self.end_headers(); self.wfile.write(body)
            return
        msg = {"role": "assistant", "content": reply}
        body = json.dumps({"id": "c1", "object": "chat.completion", "created": int(time.time()), "model": req.get("model", "mock-model"),
                           "choices": [{"index": 0, "message": msg, "finish_reason": "stop"}],
                           "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}).encode()
        self.send_response(200); self.send_header("content-type", "application/json"); self.send_header("content-length", str(len(body))); self.end_headers(); self.wfile.write(body)
ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
