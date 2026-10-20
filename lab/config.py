import os
import math
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parent.parent


def load_env(path=ROOT / ".env"):
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if sep:
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


@dataclass
class Settings:
    base_url: str = "https://openrouter.ai/api/v1"
    api_key: str = field(default="", repr=False)
    model: str = ""
    timeout: float = 40
    max_output: int = 1200
    input_price: float | None = None
    output_price: float | None = None

    @classmethod
    def from_env(cls):
        load_env()
        price = lambda name: float(os.environ[name]) if os.getenv(name, "").strip() else None
        config = cls(
            base_url=os.getenv("LLM_BASE_URL", cls.base_url).rstrip("/"),
            api_key=os.getenv("LLM_API_KEY", ""), model=os.getenv("LLM_MODEL", ""),
            timeout=float(os.getenv("LLM_TIMEOUT_SECONDS", "40")),
            max_output=int(os.getenv("LLM_MAX_OUTPUT_TOKENS", "1200")),
            input_price=price("LLM_INPUT_PRICE_PER_MILLION"),
            output_price=price("LLM_OUTPUT_PRICE_PER_MILLION"),
        )
        parsed = urlparse(config.base_url)
        if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1"}):
            raise ValueError("LLM_BASE_URL должен использовать HTTPS (HTTP допустим только для localhost).")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("LLM_BASE_URL не должен содержать учётные данные, query или fragment.")
        if not 1 <= config.timeout <= 180 or not 128 <= config.max_output <= 8192:
            raise ValueError("Недопустимый timeout или максимум выходных токенов.")
        if any(p is not None and (not math.isfinite(p) or p < 0) for p in (config.input_price, config.output_price)):
            raise ValueError("Тарифы должны быть конечными неотрицательными числами.")
        return config

    def public(self):
        return {"configured": bool(self.api_key and self.model), "base_url": self.base_url,
                "model": self.model, "timeout": self.timeout, "max_output": self.max_output,
                "price_configured": self.input_price is not None and self.output_price is not None}
