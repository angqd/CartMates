import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    data_dir: Path
    log_level: str
    free_delivery_min: int
    price_tolerance: float
    swiggy_mcp_url: str
    # Optional in M0; later milestones will require them.
    telegram_bot_token: str | None
    telegram_group_id: int | None  # negative for groups
    openai_api_key: str | None
    agent_model: str | None
    agent_reasoning_effort: str | None
    swiggy_address_id: str | None
    upi_app: str = "gpay://upi/"
    checkout_timeout_s: float = 45.0
    otel_endpoint: str | None = None   # OTLP/HTTP base URL, e.g. http://phoenix:6006; unset = tracing off
    trace_content: bool = True         # record prompts, tool args and results in spans
    trace_max_chars: int = 8000

    @property
    def db_path(self) -> Path:
        return self.data_dir / "queuify.db"

    @property
    def tokens_dir(self) -> Path:
        return self.data_dir / "tokens"


def _optional(name: str) -> str | None:
    # An empty value (as in .env.example) counts as unset.
    return os.environ.get(name) or None


def _optional_int(name: str) -> int | None:
    value = _optional(name)
    return int(value) if value is not None else None


def load_config() -> Config:
    # Real environment variables win over .env (load_dotenv does not override).
    load_dotenv()
    return Config(
        data_dir=Path(os.environ.get("DATA_DIR") or "./data"),
        log_level=os.environ.get("LOG_LEVEL") or "INFO",
        free_delivery_min=int(os.environ.get("FREE_DELIVERY_MIN") or 199),
        price_tolerance=float(os.environ.get("PRICE_TOLERANCE") or 0.05),
        swiggy_mcp_url=os.environ.get("SWIGGY_MCP_URL") or "https://mcp.swiggy.com/im",
        telegram_bot_token=_optional("TELEGRAM_BOT_TOKEN"),
        telegram_group_id=_optional_int("TELEGRAM_GROUP_ID"),
        openai_api_key=_optional("OPENAI_API_KEY"),
        agent_model=_optional("AGENT_MODEL"),
        agent_reasoning_effort=_optional("AGENT_REASONING_EFFORT"),
        swiggy_address_id=_optional("SWIGGY_ADDRESS_ID"),
        upi_app=os.environ.get("UPI_APP") or "gpay://upi/",
        checkout_timeout_s=float(os.environ.get("CHECKOUT_TIMEOUT_S") or 45),
        otel_endpoint=_optional("OTEL_EXPORTER_OTLP_ENDPOINT"),
        trace_content=(os.environ.get("QUEUIFY_TRACE_CONTENT") or "1").strip().lower() not in ("0", "false", "no", "off"),
        trace_max_chars=int(os.environ.get("QUEUIFY_TRACE_MAX_CHARS") or 8000),
    )
