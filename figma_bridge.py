import asyncio
import logging
from aiohttp import web

logger = logging.getLogger(__name__)

_pending: dict[str, dict] = {}
_results: dict[str, str] = {}
_result_events: dict[str, asyncio.Event] = {}

BRIDGE_SECRET = 'nanohatani_figma_bridge'


async def handle_poll(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return web.json_response({'session_id': None})
    if data.get('secret') != BRIDGE_SECRET:
        return web.Response(status=403)
    for sid, spec in list(_pending.items()):
        del _pending[sid]
        logger.info(f'figma_bridge: dispatching session {sid} to plugin')
        return web.json_response({'session_id': sid, 'spec': spec})
    return web.json_response({'session_id': None})


async def handle_done(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return web.Response(status=400)
    if data.get('secret') != BRIDGE_SECRET:
        return web.Response(status=403)
    sid = data.get('session_id')
    node_id = data.get('node_id', '')
    if sid:
        _results[sid] = node_id
        ev = _result_events.get(sid)
        if ev:
            ev.set()
        logger.info(f'figma_bridge: plugin done session={sid} node_id={node_id}')
    return web.json_response({'ok': True})


async def enqueue_and_wait(session_id: str, spec: dict, timeout: float = 120.0) -> str | None:
    ev = asyncio.Event()
    _result_events[session_id] = ev
    _pending[session_id] = spec
    try:
        await asyncio.wait_for(ev.wait(), timeout=timeout)
        return _results.pop(session_id, None)
    except asyncio.TimeoutError:
        _pending.pop(session_id, None)
        return None
    finally:
        _result_events.pop(session_id, None)


async def start_bridge(host: str = '0.0.0.0', port: int = 7432) -> None:
    app = web.Application()
    app.router.add_post('/figma/poll', handle_poll)
    app.router.add_post('/figma/done', handle_done)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    logger.info(f'figma_bridge: listening on {host}:{port}')
    await asyncio.Event().wait()
