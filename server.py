"""Локальный сервер без внешних зависимостей. Запуск: python server.py."""
import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from lab.config import Settings
from lab.harness import Harness
from lab.model import ChatModel
from lab.tools import SCENARIOS, TOOLS


ROOT = Path(__file__).resolve().parent


def make_server(port=8765, settings=None, model=None):
    settings = settings or Settings.from_env()
    harness = Harness(model or ChatModel(settings), settings)
    sessions = {}
    sessions_lock = threading.Lock()

    def start(session):
        with session.lock:
            if session.status == "ready" and not session.worker_running:
                session.worker_running = True
                threading.Thread(target=harness.run, args=(session,), daemon=True).start()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass  # Не печатаем диалоги и параметры запросов в консоль.

        def send(self, code, value, mime="application/json; charset=utf-8"):
            body = json.dumps(value, ensure_ascii=False).encode() if mime.startswith("application/json") else value
            self.send_response(code)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(body)

        def allowed(self):
            addresses = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
            if self.headers.get("Host") not in addresses:
                self.send(403, {"error": "Доступ только через localhost."})
                return False
            origin = self.headers.get("Origin")
            if origin and origin not in {"http://" + address for address in addresses}:
                self.send(403, {"error": "Внешний origin не разрешён."})
                return False
            return True

        def do_GET(self):
            if not self.allowed():
                return
            path = urlparse(self.path).path
            if path == "/api/config":
                return self.send(200, {"llm": settings.public(), "scenarios": SCENARIOS, "tools": TOOLS})
            if path.startswith("/api/sessions/"):
                with sessions_lock:
                    session = sessions.get(path.rsplit("/", 1)[-1])
                return self.send(200, session.public()) if session else self.send(404, {"error": "Сессия не найдена."})
            files = {"/": ("index.html", "text/html; charset=utf-8"),
                     "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                     "/style.css": ("style.css", "text/css; charset=utf-8")}
            if path not in files:
                return self.send(404, {"error": "Не найдено."})
            filename, mime = files[path]
            self.send(200, (ROOT / "web" / filename).read_bytes(), mime)

        def do_POST(self):
            if not self.allowed():
                return
            if self.headers.get("X-Lab-Request") != "1" or not self.headers.get("Content-Type", "").startswith("application/json"):
                return self.send(403, {"error": "Ожидается запрос интерфейса стенда."})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 20_000:
                    raise ValueError("Недопустимый размер запроса.")
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError("Ожидается JSON-объект.")
                path = urlparse(self.path).path
                if path == "/api/sessions":
                    if model is None and not settings.public()["configured"]:
                        raise ValueError("Сначала заполните .env: LLM_API_KEY и LLM_MODEL; затем перезапустите сервер.")
                    session = harness.new(data.get("scenario"), data.get("goal"), data.get("limits", {}))
                    with sessions_lock:
                        if len(sessions) >= 100:
                            raise ValueError("Достигнут предел 100 запусков. Скачайте нужные журналы и перезапустите сервер.")
                        sessions[session.id] = session
                    start(session)
                    return self.send(201, session.public())
                parts = path.strip("/").split("/")
                if len(parts) != 4 or parts[:2] != ["api", "sessions"]:
                    return self.send(404, {"error": "Неизвестный маршрут."})
                with sessions_lock:
                    session = sessions.get(parts[2])
                if not session:
                    return self.send(404, {"error": "Сессия не найдена."})
                if parts[3] == "resume":
                    harness.resume(session, data)
                    start(session)
                elif parts[3] == "cancel":
                    harness.cancel(session)
                elif parts[3] == "takeover":
                    with session.lock:
                        if session.status != "handoff" or not session.handoff:
                            raise ValueError("Нет ожидающей передачи.")
                        session.handoff["owner"] = "human_operator"
                        session.status = "human_owned"
                        session.event("human", "Оператор принял управление")
                else:
                    return self.send(404, {"error": "Неизвестное действие."})
                self.send(200, session.public())
            except (ValueError, TypeError) as error:
                self.send(400, {"error": str(error)})

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = make_server(args.port)
    print(f"Стенд: http://127.0.0.1:{server.server_port} — Ctrl+C для остановки.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
