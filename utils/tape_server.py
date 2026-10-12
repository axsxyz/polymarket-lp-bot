"""FastAPI surface over the local JSONL tape + Discord channel toggles.

Trading controls stay off. Bind 127.0.0.1 only.
GET / is the qv/Polymarket operator terminal.
PATCH /discord/channels is the only write surface (channel allowlist).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncIterator, Dict, List, Optional

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from loguru import logger
from pydantic import BaseModel, Field

from config.settings import DISCORD_CHANNEL_KINDS
from core.operator_digest import OperatorDigest
from core.tape import LocalTape

if TYPE_CHECKING:
    from utils.discord import DiscordNotifier

_TERMINAL_DIR = Path(__file__).resolve().parent.parent / "terminal"


class DiscordChannelsPatch(BaseModel):
    """Full replace of enabled Discord kinds (unknown tokens dropped)."""

    channels: List[str] = Field(default_factory=list)


def create_tape_app(
    tape: LocalTape,
    digest: Optional[OperatorDigest] = None,
    static_dir: Optional[Path] = None,
    discord: Optional["DiscordNotifier"] = None,
) -> FastAPI:
    app = FastAPI(title="qv/Polymarket", docs_url=None, redoc_url=None)
    ui_dir = static_dir or _TERMINAL_DIR
    live = digest if digest is not None else OperatorDigest()
    kinds_sorted = sorted(DISCORD_CHANNEL_KINDS)

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(ui_dir / "index.html", media_type="text/html")

    @app.get("/terminal.css")
    def terminal_css() -> FileResponse:
        return FileResponse(ui_dir / "terminal.css", media_type="text/css")

    @app.get("/terminal.js")
    def terminal_js() -> FileResponse:
        return FileResponse(ui_dir / "terminal.js", media_type="application/javascript")

    @app.get("/favicon.jpg")
    def favicon_jpg() -> FileResponse:
        return FileResponse(
            ui_dir / "quietvault_pfp_3.jpg",
            media_type="image/jpeg",
            content_disposition_type="inline",
        )

    @app.get("/favicon.ico")
    def favicon_ico() -> FileResponse:
        return FileResponse(
            ui_dir / "quietvault_pfp_3.jpg",
            media_type="image/jpeg",
            content_disposition_type="inline",
        )

    @app.get("/quietvault_pfp_3.jpg")
    def qv_mark() -> FileResponse:
        return FileResponse(
            ui_dir / "quietvault_pfp_3.jpg",
            media_type="image/jpeg",
            content_disposition_type="inline",
        )

    @app.get("/health")
    def health() -> Dict[str, Any]:
        return {
            "ok": True,
            "tape": str(tape.path),
            "tape_ok": tape.ok,
            "seq": tape.seq,
        }

    @app.get("/fills")
    def fills(limit: int = Query(default=100, ge=1, le=2000)) -> List[Dict[str, Any]]:
        return tape.read_events(types=["fill"], limit=limit)

    @app.get("/pnl")
    def pnl(limit: int = Query(default=50, ge=1, le=500)) -> Dict[str, Any]:
        snaps = tape.read_events(types=["snapshot"], limit=limit)
        latest_by_cid: Dict[str, Dict[str, Any]] = {}
        realised = None
        for row in snaps:
            cid = str(row.get("cid", ""))
            if cid:
                latest_by_cid[cid] = row
            if row.get("realised") is not None:
                realised = row.get("realised")
        blob = live.to_dict().get("pnl") or {}
        return {
            "realised": realised,
            "markets": list(latest_by_cid.values()),
            "snapshots_tail": snaps,
            "session_quoted": blob.get("session_quoted"),
            "session_filled": blob.get("session_filled"),
        }

    @app.get("/digest")
    def digest_route() -> Dict[str, Any]:
        return live.to_dict()

    @app.get("/events")
    def events(
        since: int = Query(default=0, ge=0),
        types: Optional[str] = Query(default=None),
        limit: int = Query(default=200, ge=1, le=2000),
    ) -> Dict[str, Any]:
        type_list = [t.strip() for t in types.split(",") if t.strip()] if types else None
        rows = tape.events_since(since=since, types=type_list, limit=limit)
        return {"seq": tape.seq, "events": rows}

    def _discord_status() -> Dict[str, Any]:
        if discord is None:
            return {
                "webhook": False,
                "kinds": kinds_sorted,
                "channels": [],
                "enabled": {k: False for k in kinds_sorted},
                "note": "discord not wired",
            }
        enabled = {k: discord.channel_enabled(k) for k in kinds_sorted}
        return {
            "webhook": discord.webhook_configured,
            "kinds": kinds_sorted,
            "channels": sorted(discord.channels),
            "enabled": enabled,
            "note": "saved to data/discord_channels.json — survives restart (.env is boot default only)",
        }

    @app.get("/discord/channels")
    def discord_channels_get() -> Dict[str, Any]:
        return _discord_status()

    @app.patch("/discord/channels")
    def discord_channels_patch(body: DiscordChannelsPatch) -> Any:
        if discord is None:
            return JSONResponse({"error": "discord not wired"}, status_code=503)
        discord.set_channels(body.channels)
        return _discord_status()

    @app.get("/stream")
    async def stream() -> StreamingResponse:
        async def gen() -> AsyncIterator[str]:
            q = tape.subscribe()
            try:
                yield ": ok\n\n"
                while True:
                    try:
                        ev = await asyncio.wait_for(q.get(), timeout=15.0)
                        payload = json.dumps(ev, default=str)
                        seq = ev.get("seq", 0)
                        yield f"id: {seq}\ndata: {payload}\n\n"
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"
            except asyncio.CancelledError:
                pass
            finally:
                tape.unsubscribe(q)

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.api_route("/{path:path}", methods=["POST", "PUT", "PATCH", "DELETE"])
    def _reject_writes(path: str) -> JSONResponse:
        return JSONResponse({"error": "read-only"}, status_code=405)

    return app


async def run_tape_server(
    tape: LocalTape,
    port: int,
    digest: Optional[OperatorDigest] = None,
    discord: Optional["DiscordNotifier"] = None,
) -> None:
    """Serve the tape on 127.0.0.1 as an asyncio background task.

    Bind failure logs and returns — the bot keeps running.
    CancelledError on shutdown / SSE disconnect is normal — do not ERROR-log it.
    """
    import logging
    import uvicorn

    class _SuppressCancelled(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            if record.exc_info:
                exc_type = record.exc_info[0]
                if exc_type is not None and issubclass(exc_type, asyncio.CancelledError):
                    return False
            try:
                if "CancelledError" in record.getMessage():
                    return False
            except Exception:
                pass
            return True

    for name in ("uvicorn", "uvicorn.error", "uvicorn.asgi"):
        logging.getLogger(name).addFilter(_SuppressCancelled())

    server: Optional[uvicorn.Server] = None
    try:
        app = create_tape_app(tape, digest=digest, discord=discord)
        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="warning",
            access_log=False,
            lifespan="off",
        )
        server = uvicorn.Server(config)
        await server.serve()
    except asyncio.CancelledError:
        if server is not None:
            server.should_exit = True
        logger.debug("Tape HTTP stopped")
    except Exception as exc:
        logger.error("Tape HTTP failed (bot continues): {}", exc)
