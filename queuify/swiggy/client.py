"""One MCP session per turn to the Swiggy Instamart server."""
import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError

from queuify import tracing
from queuify.swiggy.auth import SwiggyAuth
from queuify.swiggy.errors import LoggedOut, SwiggyToolError, SwiggyUnavailable

log = logging.getLogger(__name__)

REQUIRED_TOOLS = {
    "get_addresses", "search_products", "your_go_to_items", "get_cart", "update_cart",
    "checkout", "get_orders", "get_delivery_status", "track_order",
}
RATE_LIMIT_RETRIES = 2
JSONRPC_RATE_LIMITED = -32000


class HttpStatus(Exception):
    """The server answered with an HTTP error. The SDK hides the status inside an
    ExceptionGroup, so we capture it ourselves (see _open) and re-raise it as this."""

    def __init__(self, status: int, retry_after: float | None = None):
        super().__init__(f"HTTP {status}")
        self.status = status
        self.retry_after = retry_after


def parse_result(result) -> dict:
    """structuredContent if present, else the last text block that parses as JSON.
    The first text block is a display instruction for an LLM; we never use it."""
    texts = [b.text for b in result.content if getattr(b, "type", None) == "text"]
    if result.is_error:
        raise SwiggyToolError(texts[-1] if texts else "tool error")
    if result.structured_content:
        return result.structured_content
    for text in reversed(texts):
        try:
            parsed = json.loads(text)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise SwiggyToolError("tool result had no JSON")


def _leaves(exc: BaseException):
    if isinstance(exc, BaseExceptionGroup):
        for e in exc.exceptions:
            yield from _leaves(e)
    else:
        yield exc


class SwiggyClient:
    def __init__(self, auth: SwiggyAuth, url: str,
                 transport: httpx2.AsyncBaseTransport | None = None,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):
        self.auth = auth
        self.url = url
        self._transport = transport
        self._sleep = sleep
        self._stack: AsyncExitStack | None = None  # set while a turn is open
        self._session = None
        self._tools_checked = False
        self._last_http: HttpStatus | None = None

    # ---- turn and session ----

    @asynccontextmanager
    async def turn(self):
        """The session opens lazily on the first call and closes when the turn ends."""
        self._stack = AsyncExitStack()
        try:
            yield self
        finally:
            await self._close()
            self._stack = None

    async def _close(self) -> None:
        stack, self._session = self._stack, None
        if stack:
            self._stack = AsyncExitStack()
            try:
                await stack.aclose()
            except Exception as e:  # a broken session must not hide the real outcome
                log.debug("closing the session failed: %s", type(e).__name__)

    async def reset_session(self) -> None:
        """Drop the current session; the next call opens a fresh one (used after a call timed out)."""
        await self._close()

    async def _open(self, stack: AsyncExitStack, token: str):
        async def remember(response: httpx2.Response) -> None:
            if response.status_code >= 400:
                after = response.headers.get("retry-after", "")
                self._last_http = HttpStatus(response.status_code, float(after) if after.isdigit() else None)

        # If setup fails, `async with` unwinds the half-built transport right here. That lets
        # the SDK turn its internal cancellation into a normal exception; leaving it to the
        # turn's stack would let the raw CancelledError escape.
        async with AsyncExitStack() as building:
            # The bearer header goes on the httpx2 client we hand to the transport, so every
            # request of the session (initialize, tool calls, DELETE) carries it.
            http = await building.enter_async_context(httpx2.AsyncClient(
                headers={"Authorization": f"Bearer {token}"},
                timeout=httpx2.Timeout(30, read=300),
                event_hooks={"response": [remember]},
                transport=self._transport,
            ))
            read, write = await building.enter_async_context(
                streamable_http_client(self.url, http_client=http))
            session = await building.enter_async_context(ClientSession(read, write))
            await session.initialize()
            stack.push_async_exit(building.pop_all())  # success: the turn now owns it
        return session

    def _translate(self, exc: Exception) -> Exception:
        """Turn whatever the SDK raised into HttpStatus, SwiggyUnavailable, or the original."""
        if isinstance(exc, (HttpStatus, SwiggyToolError, LoggedOut, SwiggyUnavailable)):
            return exc
        leaves = list(_leaves(exc))
        for leaf in leaves:
            if isinstance(leaf, MCPError) and leaf.code == JSONRPC_RATE_LIMITED:
                return HttpStatus(429, self._last_http.retry_after if self._last_http else None)
        if self._last_http:
            return self._last_http
        if any(isinstance(leaf, (httpx2.TransportError, OSError)) for leaf in leaves):
            return SwiggyUnavailable(type(leaves[0]).__name__)
        return exc

    async def _ensure_session(self):
        if self._session is None:
            token = await self.auth.access_token()
            self._session = await self._open(self._stack, token)
            if not self._tools_checked:
                self._tools_checked = True  # only after a session worked, not at boot (may be logged out)
                await self._check_tools()
        return self._session

    async def _check_tools(self) -> None:
        try:
            listed = await self._session.list_tools()
        except Exception as e:
            log.warning("could not list tools: %s", type(e).__name__)
            return
        missing = REQUIRED_TOOLS - {t.name for t in listed.tools}
        if missing:
            log.error("Swiggy server is missing required tools: %s", ", ".join(sorted(missing)))

    # ---- calling ----

    async def call(self, tool: str, args: dict, retry_rate_limit: bool = True) -> dict:
        """retry_rate_limit=False for calls that must run at most once (checkout).
        Traced as "swiggy:<tool>": the arguments and result, never headers or tokens."""
        with tracing.span(f"swiggy:{tool}", tracing.CHAIN, {"swiggy.tool": tool}) as sp:
            tracing.set_json(sp, "input.value", args)
            try:
                result = await self._call(sp, tool, args, retry_rate_limit)
            except Exception as e:
                sp.set_attribute("swiggy.status", type(e).__name__)
                raise
            sp.set_attribute("swiggy.status", "ok")
            sp.set_attribute("swiggy.result_chars", len(tracing.dump(result)))
            tracing.set_json(sp, "output.value", result)
            return result

    async def _call(self, sp, tool: str, args: dict, retry_rate_limit: bool) -> dict:
        if self._stack is None:  # outside a turn: a one-off session
            async with self.turn():
                return await self._call(sp, tool, args, retry_rate_limit)
        refreshed, rate_retries = False, 0
        while True:
            self._last_http = None
            try:
                session = await self._ensure_session()
                return parse_result(await session.call_tool(tool, args))
            except Exception as e:
                err = self._translate(e)
                if err is not e:
                    log.debug("swiggy %s: %s", tool, err)
                if not isinstance(err, HttpStatus):
                    raise err from None
                if err.status == 401:
                    if refreshed:
                        self.auth.mark_logged_out()
                        raise LoggedOut("Swiggy still rejects the token") from None
                    # Retrying is safe here only because a 401 means the server rejected the
                    # request before executing it. Checkout also passes retry_rate_limit=False.
                    refreshed = True
                    sp.add_event("token_refresh")
                    await self._close()  # the old session carries the old bearer token
                    await self.auth.force_refresh()
                elif err.status == 429 and retry_rate_limit and rate_retries < RATE_LIMIT_RETRIES:
                    rate_retries += 1
                    sp.add_event("rate_limited", {"retry": rate_retries})
                    await self._sleep(min(err.retry_after or 2 * rate_retries, 30))
                else:
                    raise SwiggyUnavailable(str(err)) from None
