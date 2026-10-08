"""FastAPI application for AnkaLoop Server."""

from __future__ import annotations

import logging
import secrets
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .._version import __version__
from ..application_services import ApplicationServices
from ..llm import ProviderError, ProviderErrorKind
from ..tracing import attach_carrier, carrier_from_headers, configure_tracing_from_config, shutdown_tracing
from .config import AuthConfig, ServerConfig, get_server_config, set_server_config
from .events import router as events_router
from .routes import agents_router, health_router, sessions_router, tools_router
from .session_manager import SessionManager, set_session_manager
from .websocket import router as websocket_router

logger = logging.getLogger(__name__)

# Global app instance
_app: FastAPI | None = None


def _request_api_key(request: Request) -> str | None:
    """Extract an API key from supported HTTP authentication headers."""
    authorization = request.headers.get("Authorization", "")
    scheme, _, credential = authorization.partition(" ")
    if scheme.lower() == "bearer" and credential:
        return credential
    return request.headers.get("X-API-Key")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan handler.

    Handles startup and shutdown events.
    """
    # Startup
    config = get_server_config()
    configure_tracing_from_config()
    print(f"🚀 AnkaLoop Server v{__version__} starting...")
    print(f"   Host: {config.host}")
    print(f"   Port: {config.port}")
    print(f"   Working directory: {config.work_dir or 'current directory'}")

    # Initialize session manager
    services: ApplicationServices = app.state.application_services
    session_manager = SessionManager(config, services)
    set_session_manager(session_manager)

    # Enable skill hot reload for server runtime.
    from ..skills import SkillWatcher

    skill_manager = services.skill_manager
    skill_manager.discover_skills(config.work_dir)
    skill_watcher = SkillWatcher(skill_manager)
    await skill_watcher.start(project_root=config.work_dir)
    print("   Skill hot reload: enabled")

    yield

    # Shutdown
    if skill_watcher:
        await skill_watcher.stop()

    shutdown_tracing()
    print("\n👋 AnkaLoop Server shutting down...")


def create_app(
    config: ServerConfig | None = None,
    services: ApplicationServices | None = None,
) -> FastAPI:
    """Create and configure the FastAPI application.

    Args:
        config: Optional server configuration. Uses default if not provided.

    Returns:
        Configured FastAPI application.
    """
    global _app

    # Set configuration
    if config:
        set_server_config(config)

    cfg = get_server_config()
    cfg.validate_security()
    application_services = services or ApplicationServices.default()
    set_session_manager(SessionManager(cfg, application_services))

    # Create FastAPI app
    app = FastAPI(
        title="AnkaLoop Server",
        description="AI Coding Agent Server with HTTP/WebSocket API",
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )
    app.state.server_config = cfg
    app.state.application_services = application_services

    @app.middleware("http")
    async def propagate_trace_context(request: Request, call_next):
        """Join an incoming W3C ``traceparent`` so turn spans nest under the caller."""
        with attach_carrier(carrier_from_headers(request.headers)):
            return await call_next(request)

    # Configure CORS
    if cfg.cors.enabled:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cfg.cors.allow_origins,
            allow_credentials=cfg.cors.allow_credentials,
            allow_methods=cfg.cors.allow_methods,
            allow_headers=cfg.cors.allow_headers,
        )

    if cfg.auth.enabled:
        expected_key = cfg.auth.api_key or ""

        @app.middleware("http")
        async def require_api_key(request: Request, call_next):
            """Authenticate all versioned API routes except the health probe."""
            if request.url.path.startswith("/api/v1/") and request.url.path != "/api/v1/health":
                supplied_key = _request_api_key(request)
                if supplied_key is None or not secrets.compare_digest(supplied_key, expected_key):
                    return JSONResponse(
                        status_code=401,
                        content={"error": "Invalid or missing API key", "code": "UNAUTHORIZED"},
                        headers={"WWW-Authenticate": "Bearer"},
                    )
            return await call_next(request)

    # Add exception handlers
    @app.exception_handler(ProviderError)
    async def provider_exception_handler(request: Request, exc: ProviderError) -> JSONResponse:
        """Return stable provider diagnostics without leaking SDK exception details."""
        status_by_kind = {
            ProviderErrorKind.CONTEXT_OVERFLOW: 400,
            ProviderErrorKind.RATE_LIMIT: 503,
            ProviderErrorKind.TIMEOUT: 504,
        }
        return JSONResponse(
            status_code=status_by_kind.get(exc.kind, 502),
            content={
                "error": str(exc),
                "code": f"PROVIDER_{exc.kind.value.upper()}",
                "retryable": exc.retryable,
                "upstream_status": exc.status_code,
            },
        )

    @app.exception_handler(Exception)
    async def general_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        """Handle uncaught exceptions."""
        return JSONResponse(
            status_code=500,
            content={
                "error": str(exc),
                "code": "INTERNAL_ERROR",
                "details": None,
            },
        )

    # Include routers with /api/v1 prefix
    api_prefix = "/api/v1"
    app.include_router(health_router, prefix=api_prefix)
    app.include_router(sessions_router, prefix=api_prefix)
    app.include_router(tools_router, prefix=api_prefix)
    app.include_router(agents_router, prefix=api_prefix)
    app.include_router(events_router, prefix=api_prefix)

    # Include WebSocket router (at root level)
    app.include_router(websocket_router)

    # Add root endpoint
    @app.get("/")
    async def root():
        """Root endpoint with API information."""
        return {
            "name": "ankaloop-server",
            "version": __version__,
            "api": f"{api_prefix}",
            "docs": "/docs",
            "health": f"{api_prefix}/health",
        }

    # Store app globally
    _app = app

    return app


def get_app() -> FastAPI:
    """Get the global FastAPI application.

    Creates a new app with default config if not yet created.

    Returns:
        The FastAPI application.
    """
    global _app
    if _app is None:
        _app = create_app()
    return _app


def run_server(
    host: str = "127.0.0.1",
    port: int = 8080,
    work_dir: str | None = None,
    reload: bool = False,
    auth: AuthConfig | None = None,
) -> None:
    """Run the AnkaLoop server.

    Args:
        host: Host to bind to.
        port: Port to listen on.
        work_dir: Working directory for sessions.
        reload: Enable auto-reload for development.
        auth: Authentication config from the application configuration.
    """
    from pathlib import Path

    import uvicorn

    # Create configuration
    config = ServerConfig(
        host=host,
        port=port,
        work_dir=Path(work_dir) if work_dir else None,
        auth=auth or AuthConfig(),
    )
    config.validate_security()
    set_server_config(config)

    # Create app
    app = create_app(config)

    # Run with uvicorn
    uvicorn.run(
        app,
        host=host,
        port=port,
        reload=reload,
        log_level="info",
    )
