import json
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from lab.config import Settings
from lab.model import ChatModel, ModelError
from server import make_server
from tests.test_harness import DONE, FakeModel, tool


class ProtocolTests(unittest.TestCase):
    def test_real_http_client_sends_tools_and_keeps_tool_result_ids(self):
        received = []
        class Provider(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"choices": [{"message": tool("get_order", order_id="42"), "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 10, "completion_tokens": 5}}).encode())
        server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            model = ChatModel(Settings(base_url=f"http://127.0.0.1:{server.server_port}/v1", api_key="test-only", model="test-model"))
            from lab.tools import TOOLS
            messages = [{"role": "user", "content": "Заказ 42"}]
            answer, usage = model.complete(messages, TOOLS, 5)
            messages.extend([answer, {"role": "tool", "tool_call_id": "call-get_order", "content": "{}"}])
            model.complete(messages, TOOLS, 5)
            self.assertEqual(received[1]["messages"][-1]["tool_call_id"], "call-get_order")
            self.assertEqual(received[1]["tools"], TOOLS)
            self.assertEqual(received[0]["model"], "test-model")
            self.assertEqual(usage["prompt_tokens"], 10)
        finally:
            server.shutdown()
            server.server_close()

    def test_http_session_approval_and_no_key_in_public_config(self):
        fake = FakeModel([tool("prepare_refund", order_id="42"), tool("create_refund", order_id="42", amount=3200), DONE])
        server = make_server(0, Settings(api_key="never-expose-this", model="test"), fake)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}"
        def request(path, data=None, headers=None):
            req = urllib.request.Request(base+path, data=json.dumps(data).encode() if data is not None else None,
                    headers=headers or {"Content-Type": "application/json", "X-Lab-Request": "1"})
            with urllib.request.urlopen(req, timeout=5) as response:
                return json.load(response)
        def settle(s):
            deadline = time.monotonic()+5
            while s["busy"] and time.monotonic()<deadline:
                time.sleep(.01)
                s=request("/api/sessions/"+s["id"])
            return s
        try:
            config = request("/api/config")
            self.assertNotIn("never-expose-this", json.dumps(config))
            s=settle(request("/api/sessions", {"scenario": "normal", "goal": "Возврат 42"}))
            self.assertEqual(s["status"], "awaiting_approval")
            self.assertEqual(s["state"]["refunds"], [])
            p=s["pending"]
            s=settle(request("/api/sessions/"+s["id"]+"/resume", {"pending_id":p["id"],"decision":"approve","args":p["args"]}))
            self.assertEqual(s["status"], "completed")
            with self.assertRaises(urllib.error.HTTPError) as error:
                request("/api/sessions", {}, {"Content-Type":"application/json", "X-Lab-Request":"1", "Origin":"https://example.com"})
            self.assertEqual(error.exception.code,403)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
