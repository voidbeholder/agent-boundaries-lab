"""Только транспорт к LLM. Здесь нет правил возврата или согласования."""
import json
import socket
import urllib.error
import urllib.request


class ModelError(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Не пересылаем ключ на адрес из HTTP-redirect.
        return None


class ChatModel:
    def __init__(self, settings):
        self.settings = settings
        self.opener = urllib.request.build_opener(NoRedirect)

    def complete(self, messages, tools, timeout):
        if not self.settings.api_key or not self.settings.model:
            raise ModelError("Заполните LLM_API_KEY и LLM_MODEL в .env и перезапустите сервер.")
        payload = {"model": self.settings.model, "messages": messages, "tools": tools,
                   "tool_choice": "auto", "max_tokens": self.settings.max_output, "stream": False}
        request = urllib.request.Request(
            self.settings.base_url + "/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode(), method="POST",
            headers={"Authorization": "Bearer " + self.settings.api_key,
                     "Content-Type": "application/json", "User-Agent": "agent-boundaries-lab/0.1"},
        )
        try:
            # Намеренно без автоматических повторов: один ход = один HTTP-запрос.
            with self.opener.open(request, timeout=min(timeout, self.settings.timeout)) as response:
                data = json.loads(response.read(2_000_000))
        except urllib.error.HTTPError as error:
            hints = {401: "Проверьте ключ API.", 402: "Проверьте баланс или бесплатный маршрут.",
                     403: "Проверьте права ключа и доступ к модели.",
                     404: "Проверьте адрес API и идентификатор модели.",
                     429: "Квота провайдера исчерпана; это не бюджет задачи в harness.",
                     400: "Проверьте поддержку tools и параметров Chat Completions выбранной моделью."}
            raise ModelError(f"API вернул HTTP {error.code}. " + hints.get(error.code, "Ошибка провайдера; автоматических повторов нет.")) from None
        except (urllib.error.URLError, TimeoutError, socket.timeout):
            raise ModelError("API не ответил в отведённое время или недоступен. Повтор автоматически не выполнялся.") from None
        except (ValueError, OSError):
            raise ModelError("Не удалось прочитать ответ API.") from None
        try:
            choice = data["choices"][0]
            if choice.get("finish_reason") == "length":
                raise ModelError("Ответ обрезан лимитом выходных токенов. Выберите больший LLM_MAX_OUTPUT_TOKENS.")
            raw = choice["message"]
            message = {"role": "assistant", "content": raw.get("content") or ""}
            if not isinstance(message["content"], str):
                raise ValueError()
            if raw.get("tool_calls"):
                message["tool_calls"] = raw["tool_calls"]
            # Некоторые провайдеры требуют возврата reasoning_details при tool calling.
            # Сохраняем только в API-истории; не выводим в пользовательский журнал.
            for key in ("reasoning_details", "reasoning_content"):
                if raw.get(key):
                    message[key] = raw[key]
            return message, data.get("usage") or {}
        except (KeyError, IndexError, TypeError, ValueError):
            raise ModelError("Провайдер вернул неожиданный формат Chat Completions.") from None
