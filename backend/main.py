"""
main.py — Nipate aiohttp application entry point.

Start the server:
    python -m backend.main

Or with hot-reload during development:
    pip install aiohttp-devtools
    adev runserver backend/main.py
"""

import logging
import sys
from pathlib import Path

from aiohttp import web

from backend.config import APP_HOST, APP_PORT, DEBUG
from backend.middleware.auth import cors_middleware
from backend.routes.auth import router as auth_router

# Set up structured logging
logging.basicConfig(
    level=logging.DEBUG if DEBUG else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)


def create_app() -> web.Application:
    """
    Factory function that creates and configures the aiohttp Application.
    Import other routers here as you build them out.
    """
    app = web.Application(middlewares=[cors_middleware])

    # ── Route registration ─────────────────────────────────────────────────────
    app.router.add_routes(auth_router)

    from backend.routes.cases    import router as cases_router
    from backend.routes.tips     import router as tips_router
    from backend.routes.alerts   import router as alerts_router
    from backend.routes.admin    import router as admin_router
    from backend.routes.webhooks import router as webhooks_router
    app.router.add_routes(cases_router)
    app.router.add_routes(tips_router)
    app.router.add_routes(alerts_router)
    app.router.add_routes(admin_router)
    app.router.add_routes(webhooks_router)

    # ── Static frontend files ──────────────────────────────────────────────────
    frontend_dir = Path(__file__).parent.parent / "frontend"
    if frontend_dir.exists():
        app.router.add_static("/", frontend_dir, name="static", show_index=True)

    # ── Health check ───────────────────────────────────────────────────────────
    async def health(_: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "service": "nipate-api"})

    app.router.add_get("/api/health", health)

    # ── Startup / shutdown hooks ───────────────────────────────────────────────
    async def on_startup(application: web.Application):
        log.info("Nipate API starting on %s:%s (debug=%s)", APP_HOST, APP_PORT, DEBUG)

    async def on_shutdown(application: web.Application):
        log.info("Nipate API shutting down.")

    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    return app


if __name__ == "__main__":
    application = create_app()
    web.run_app(application, host=APP_HOST, port=APP_PORT, access_log=log)
