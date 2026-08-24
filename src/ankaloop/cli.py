"""Typer CLI commands for AnkaLoop."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path
from typing import Annotated

import typer
from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from rich.console import Console
from rich.json import JSON
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from . import __git_commit__, __version__
from .agent import Agent, create_agent_by_name
from .agent_spec import get_default_agent_spec, list_available_agents, load_agent_spec
from .commands import get_command_manager
from .config import (
    AnkaloopConfig,
    Server,
    load_config,
    save_config,
    save_default_config,
)
from .constants import CONFIG_DIR_NAME
from .mcp_client import call_mcp_tool, list_mcp_tools
from .multi_agent import get_agent_registry
from .skills import get_skill_manager


def get_git_commit_hash() -> str | None:
    """Get the git commit hash.

    First checks for embedded hash (set during release builds),
    then falls back to running git command (for development).
    """
    # Use embedded hash from release build if available
    if __git_commit__:
        return __git_commit__

    # Fallback to git command for local development
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        pass
    return None


def version_callback(value: bool) -> None:
    """Display version information and exit."""
    if value:
        version_str = f"anka version {__version__}"
        git_hash = get_git_commit_hash()
        if git_hash:
            version_str += f" (git: {git_hash})"
        typer.echo(version_str)
        raise typer.Exit()


app = typer.Typer(add_completion=False, context_settings={"help_option_names": ["-h", "--help"]})
console = Console()


@app.command(help="Initialize AnkaLoop configuration with interactive wizard")
def init(
    quick: Annotated[
        bool,
        typer.Option("--quick", "-q", help="Skip interactive wizard and use default config"),
    ] = False,
) -> None:
    """Initialize AnkaLoop configuration.

    By default, runs an interactive wizard to help you configure your AI provider.
    Use --quick to skip the wizard and create a default config file.
    """
    if quick:
        path = save_default_config()
        console.print(f"[green]Wrote default config to {path}[/green]")
    else:
        from .init_wizard import run_init_wizard

        run_init_wizard()


mcp = typer.Typer(help="MCP utilities (stdio client)")
app.add_typer(mcp, name="mcp")

telegram = typer.Typer(help="Telegram bot utilities")
app.add_typer(telegram, name="telegram")


@telegram.command("start", help="Start Telegram bot")
def telegram_start(
    token: Annotated[
        str | None,
        typer.Option("--token", help="Telegram bot token"),
    ] = None,
    allow_user: Annotated[
        list[int] | None,
        typer.Option("--allow-user", "-u", help="Allowed user ID (repeatable)"),
    ] = None,
    admin_user: Annotated[
        list[int] | None,
        typer.Option("--admin-user", "-a", help="Admin user ID (repeatable)"),
    ] = None,
    webhook: Annotated[
        bool,
        typer.Option("--webhook", help="Enable webhook mode"),
    ] = False,
    webhook_url: Annotated[
        str | None,
        typer.Option("--webhook-url", help="Webhook URL"),
    ] = None,
    work_dir: Annotated[
        Path | None,
        typer.Option(
            "--work-dir",
            "-w",
            exists=True,
            file_okay=False,
            dir_okay=True,
            readable=True,
        ),
    ] = None,
) -> None:
    try:
        from .telegram.bot import TelegramBot
    except Exception as exc:  # pragma: no cover - optional dependency
        console.print(f"[red]Telegram integration unavailable: {exc}[/red]")
        raise typer.Exit(1) from exc
    from .telegram.config import TelegramConfig

    cfg = load_config()
    telegram_cfg = cfg.telegram or TelegramConfig()
    telegram_cfg.enabled = True
    if token:
        telegram_cfg.bot_token = token
    if webhook:
        telegram_cfg.webhook_mode = True
    if webhook_url:
        telegram_cfg.webhook_url = webhook_url

    bot_token = telegram_cfg.bot_token
    if not bot_token:
        console.print("[red]Missing bot token. Provide --token or set config.[/red]")
        raise typer.Exit(1)

    allowed = set(allow_user) if allow_user is not None else set(telegram_cfg.allowed_users)
    admin = set(admin_user) if admin_user is not None else set(telegram_cfg.admin_users)

    if telegram_cfg.dm_policy == "allowlist" and not allowed:
        console.print("[red]No allowed users. Provide --allow-user or set config.[/red]")
        raise typer.Exit(1)
    if telegram_cfg.dm_policy == "pairing" and not admin:
        console.print("[red]At least one admin user is required for pairing mode.[/red]")
        raise typer.Exit(1)

    bot = TelegramBot(
        token=bot_token,
        allowed_users=allowed,
        admin_users=admin,
        work_dir=work_dir,
        config=telegram_cfg,
    )

    if telegram_cfg.webhook_mode:
        if not telegram_cfg.webhook_url:
            console.print("[red]Webhook URL required for webhook mode.[/red]")
            raise typer.Exit(1)
        asyncio.run(bot.start_webhook(telegram_cfg.webhook_url))
    else:
        asyncio.run(bot.start_polling())


@telegram.command("status", help="Show Telegram bot configuration status")
def telegram_status() -> None:
    cfg = load_config()
    if not cfg.telegram:
        console.print("[yellow]Telegram not configured.[/yellow]")
        return
    summary = [
        f"enabled: {cfg.telegram.enabled}",
        f"bot_token: {'set' if cfg.telegram.bot_token else 'unset'}",
        f"allowed_users: {len(cfg.telegram.allowed_users)}",
        f"admin_users: {len(cfg.telegram.admin_users)}",
        f"webhook_mode: {cfg.telegram.webhook_mode}",
        f"dm_policy: {cfg.telegram.dm_policy}",
        f"group_policy: {cfg.telegram.group_policy}",
        f"typing_indicator: {cfg.telegram.typing_indicator}",
        f"max_queue_size: {cfg.telegram.max_queue_size}",
    ]
    console.print("\n".join(summary))


@telegram.command("setup", help="Interactive setup for Telegram bot")
def telegram_setup() -> None:
    from .telegram.config import TelegramConfig, parse_user_ids

    token = typer.prompt("Bot token", hide_input=True)
    allowed_raw = typer.prompt("Allowed user IDs (comma-separated)")
    allowed = parse_user_ids(allowed_raw)
    if not allowed:
        console.print("[red]At least one allowed user ID is required.[/red]")
        raise typer.Exit(1)
    admin_raw = typer.prompt("Admin user IDs (comma-separated)", default="")
    admin = parse_user_ids(admin_raw) or allowed

    cfg = load_config()
    if cfg.telegram is None:
        cfg.telegram = TelegramConfig()
    cfg.telegram.enabled = True
    cfg.telegram.bot_token = token
    cfg.telegram.allowed_users = allowed
    cfg.telegram.admin_users = admin

    path = save_config(cfg)
    try:
        path.chmod(0o600)
    except OSError:
        console.print("[yellow]Warning: could not set config file permissions.[/yellow]")
    console.print(f"[green]Telegram config saved to {path}[/green]")


@app.command("serve", help="Start AnkaLoop server for remote access via HTTP/WebSocket")
def serve_command(
    host: Annotated[
        str,
        typer.Option(
            "--host",
            "-H",
            envvar="ANKA_HOST",
            help="Host to bind the server to (env: ANKA_HOST)",
        ),
    ] = "127.0.0.1",
    port: Annotated[
        int,
        typer.Option(
            "--port",
            "-p",
            envvar="ANKA_PORT",
            help="Port to listen on (env: ANKA_PORT)",
        ),
    ] = 8080,
    api_key: Annotated[
        str | None,
        typer.Option(
            "--api-key",
            envvar="ANKA_API_KEY",
            help="Enable server authentication with this API key (env: ANKA_API_KEY)",
        ),
    ] = None,
    work_dir: Annotated[
        Path | None,
        typer.Option(
            "--work-dir",
            "-w",
            help="Default working directory for sessions",
            exists=True,
            file_okay=False,
            dir_okay=True,
            readable=True,
        ),
    ] = None,
    reload: Annotated[
        bool,
        typer.Option("--reload", help="Enable auto-reload for development"),
    ] = False,
    telegram_enabled: Annotated[
        bool,
        typer.Option("--telegram", help="Start Telegram bot alongside the server"),
    ] = False,
) -> None:
    """Start AnkaLoop as an HTTP/WebSocket server.

    This allows remote clients to connect and interact with AnkaLoop agents.
    AnkaLoop does not start in-process daemon background orchestration.

    Examples:
        anka serve                          # Start on localhost:8080
        anka serve --port 8080             # Use custom port
        anka serve --host 0.0.0.0 --api-key s3cret  # Listen on all interfaces
        anka serve -w /path/to/project     # Set default working directory
        ANKA_HOST=0.0.0.0 ANKA_API_KEY=... anka serve  # Same via env vars

    API Documentation:
        Once running, visit http://localhost:8080/docs for API docs.

    Connect with:
        anka attach http://localhost:8080   # Connect CLI to running server

    Deploy with Docker or systemd for production.
    """
    from .server import run_server
    from .server.config import AuthConfig as RuntimeAuthConfig

    cfg = load_config()
    configured_auth = cfg.server.auth if cfg.server else None
    if api_key:
        runtime_auth = RuntimeAuthConfig(enabled=True, api_key=api_key)
    elif configured_auth:
        runtime_auth = RuntimeAuthConfig(
            enabled=configured_auth.enabled,
            api_key=configured_auth.api_key,
        )
    else:
        runtime_auth = RuntimeAuthConfig()

    if telegram_enabled:
        _start_telegram_bot(work_dir)

    console.print("[green]Running without in-process daemon background orchestration.[/green]")

    run_server(
        host=host,
        port=port,
        work_dir=str(work_dir) if work_dir else None,
        reload=reload,
        auth=runtime_auth,
    )


def _start_telegram_bot(work_dir: Path | None) -> None:
    cfg = load_config()
    if not cfg.telegram or not cfg.telegram.bot_token:
        console.print("[red]Telegram bot token missing in config[/red]")
        raise typer.Exit(1)
    if not cfg.telegram.allowed_users:
        console.print("[red]Telegram allowed_users list is empty[/red]")
        raise typer.Exit(1)

    try:
        from .telegram.bot import TelegramBot
    except Exception as exc:  # pragma: no cover - optional dependency
        console.print(f"[red]Telegram integration unavailable: {exc}[/red]")
        raise typer.Exit(1) from exc

    token = cfg.telegram.bot_token
    allowed = set(cfg.telegram.allowed_users)
    admin = set(cfg.telegram.admin_users)
    telegram_cfg = cfg.telegram

    def run_bot() -> None:
        try:
            bot = TelegramBot(
                token=token,
                allowed_users=allowed,
                admin_users=admin,
                work_dir=work_dir,
                config=telegram_cfg,
            )
            if telegram_cfg.webhook_mode and telegram_cfg.webhook_url:
                asyncio.run(bot.start_webhook(telegram_cfg.webhook_url))
            else:
                asyncio.run(bot.start_polling())
        except Exception:
            console.print("[red]Telegram bot failed to start[/red]")

    import threading

    thread = threading.Thread(target=run_bot, daemon=True)
    thread.start()
    console.print("[green]Telegram bot started in background[/green]")


@app.command("attach", help="Connect to a running AnkaLoop server")
def attach_command(
    url: Annotated[
        str,
        typer.Argument(help="URL of the AnkaLoop server (e.g., http://localhost:8080)"),
    ],
    session_id: Annotated[
        str | None,
        typer.Option("--session", "-s", help="Session ID to connect to or create"),
    ] = None,
    work_dir: Annotated[
        Path | None,
        typer.Option(
            "--work-dir",
            "-w",
            help="Working directory for new sessions",
            exists=True,
            file_okay=False,
            dir_okay=True,
            readable=True,
        ),
    ] = None,
    api_key: Annotated[
        str | None,
        typer.Option(
            "--api-key",
            help="Server API key (or set ANKA_SERVER_API_KEY)",
            envvar="ANKA_SERVER_API_KEY",
        ),
    ] = None,
) -> None:
    """Connect to a running AnkaLoop server and interact with it.

    This allows you to control an AnkaLoop server running on another machine
    or in another process.

    Examples:
        anka attach http://localhost:8080
        anka attach http://remote-server:8080 --session my-session
        anka attach http://localhost:8080 -w /path/to/project
    """
    # Use synchronous implementation to avoid event loop issues
    _attach_sync(url, session_id, work_dir, api_key)


def _attach_sync(
    url: str,
    session_id: str | None,
    work_dir: Path | None,
    api_key: str | None = None,
) -> None:
    """Synchronous implementation of attach command using httpx directly.

    This avoids event loop issues in interactive environments.
    """
    import httpx
    from prompt_toolkit import PromptSession
    from prompt_toolkit.history import FileHistory

    base_url = url.rstrip("/")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    # Check server health
    try:
        with httpx.Client(timeout=5.0, headers=headers) as http:
            health_resp = http.get(f"{base_url}/api/v1/health")
            health_resp.raise_for_status()
            health = health_resp.json()
            console.print(f"[green]Connected to AnkaLoop Server v{health.get('version', 'unknown')}[/green]")
    except Exception as e:
        console.print(f"[red]Failed to connect to server: {e}[/red]")
        raise typer.Exit(1) from None

    # Create or get session
    try:
        with httpx.Client(timeout=30.0, headers=headers) as http:
            if session_id:
                # Try to get existing session
                try:
                    sess_resp = http.get(f"{base_url}/api/v1/sessions/{session_id}")
                    sess_resp.raise_for_status()
                    session_data = sess_resp.json()
                    console.print(f"[dim]Resumed session: {session_id}[/dim]")
                except httpx.HTTPStatusError:
                    # Create new session
                    sess_resp = http.post(
                        f"{base_url}/api/v1/sessions",
                        json={"cwd": str(work_dir) if work_dir else None},
                    )
                    sess_resp.raise_for_status()
                    session_data = sess_resp.json()
                    session_id = session_data["id"]
                    console.print(f"[dim]Created session: {session_id}[/dim]")
            else:
                # Create new session
                sess_resp = http.post(
                    f"{base_url}/api/v1/sessions",
                    json={"cwd": str(work_dir) if work_dir else None},
                )
                sess_resp.raise_for_status()
                session_data = sess_resp.json()
                session_id = session_data["id"]
                console.print(f"[dim]Created session: {session_id}[/dim]")
    except Exception as e:
        console.print(f"[red]Failed to create session: {e}[/red]")
        raise typer.Exit(1) from None

    console.print("[bold]AnkaLoop Remote Client[/bold]")
    console.print(f"[dim]Server: {url}[/dim]")
    console.print(f"[dim]Session: {session_id}[/dim]")
    console.print("[dim]Type /exit to quit, /help for commands[/dim]")
    console.print()

    # Setup prompt
    history_file = Path.home() / ".config" / CONFIG_DIR_NAME / "attach_history.txt"
    history_file.parent.mkdir(parents=True, exist_ok=True)
    prompt_session: PromptSession[str] = PromptSession(history=FileHistory(str(history_file)))

    while True:
        try:
            user_input = prompt_session.prompt("You: ").strip()

            if not user_input:
                continue

            if user_input.lower() in ["/exit", "/quit", "exit", "quit"]:
                console.print("[green]Goodbye! 👋[/green]")
                break

            if user_input.lower() == "/help":
                console.print("[bold]Commands:[/bold]")
                console.print("  /exit, /quit  - Exit the client")
                console.print("  /new          - Create and switch to a new session")
                console.print("  /sessions     - List all sessions")
                console.print("  /session switch <id> - Switch to a session")
                console.print("  /info         - Show session info")
                console.print("  /tools        - List available tools")
                console.print("  /agents       - List available agents")
                console.print("  /cancel       - Cancel current operation in session")
                console.print()
                continue

            if user_input.lower() in {"/new", "/session new"}:
                try:
                    with httpx.Client(timeout=30.0, headers=headers) as http:
                        sess_resp = http.post(
                            f"{base_url}/api/v1/sessions",
                            json={"cwd": str(work_dir) if work_dir else None},
                        )
                        sess_resp.raise_for_status()
                        session_data = sess_resp.json()
                        session_id = session_data["id"]
                        console.print(f"[green]Created session: {session_id}[/green]")
                except Exception as e:
                    console.print(f"[red]Failed to create session: {e}[/red]")
                console.print()
                continue

            if user_input.lower().startswith("/session switch "):
                target_session_id = user_input.split(maxsplit=2)[2].strip()
                try:
                    with httpx.Client(timeout=10.0, headers=headers) as http:
                        resp = http.get(f"{base_url}/api/v1/sessions/{target_session_id}")
                        resp.raise_for_status()
                        session_id = target_session_id
                        console.print(f"[green]Switched to session: {session_id}[/green]")
                except Exception as e:
                    console.print(f"[red]Failed to switch session: {e}[/red]")
                console.print()
                continue

            if user_input.lower() == "/cancel":
                try:
                    with httpx.Client(timeout=5.0, headers=headers) as http:
                        http.post(f"{base_url}/api/v1/sessions/{session_id}/cancel")
                        console.print("[green]Cancel request sent.[/green]")
                except Exception as e:
                    console.print(f"[red]Failed to send cancel request: {e}[/red]")
                console.print()
                continue

            if user_input.lower() == "/sessions":
                with httpx.Client(timeout=10.0, headers=headers) as http:
                    resp = http.get(f"{base_url}/api/v1/sessions")
                    sessions = resp.json()
                    console.print("[bold]Sessions:[/bold]")
                    for s in sessions.get("sessions", []):
                        status_icon = "🔄" if s.get("status") == "busy" else "✅"
                        console.print(f"  {status_icon} {s['id']} - {s.get('status', 'unknown')}")
                console.print()
                continue

            if user_input.lower() == "/info":
                with httpx.Client(timeout=10.0, headers=headers) as http:
                    resp = http.get(f"{base_url}/api/v1/sessions/{session_id}")
                    session_info = resp.json()
                    console.print("[bold]Session Info:[/bold]")
                    console.print(f"  ID: {session_info['id']}")
                    console.print(f"  Status: {session_info.get('status', 'unknown')}")
                    console.print(f"  Agent: {session_info.get('agent_name', 'unknown')}")
                    console.print(f"  Messages: {session_info.get('message_count', 0)}")
                console.print()
                continue

            if user_input.lower() == "/tools":
                with httpx.Client(timeout=10.0, headers=headers) as http:
                    resp = http.get(f"{base_url}/api/v1/tools")
                    tools_data = resp.json()
                    tools = tools_data.get("tools", [])
                    console.print("[bold]Available Tools:[/bold]")
                    for tool in tools[:20]:
                        console.print(f"  • {tool['name']}: {tool.get('description', '')[:50]}")
                    if len(tools) > 20:
                        console.print(f"  ... and {len(tools) - 20} more")
                console.print()
                continue

            if user_input.lower() == "/agents":
                with httpx.Client(timeout=10.0, headers=headers) as http:
                    resp = http.get(f"{base_url}/api/v1/agents")
                    agents_data = resp.json()
                    agents = agents_data.get("agents", [])
                    console.print("[bold]Available Agents:[/bold]")
                    for agent_info in agents:
                        console.print(f"  • {agent_info['name']}: {agent_info.get('description', '')[:50]}")
                console.print()
                continue

            # Send prompt to server with streaming
            try:
                with (
                    httpx.Client(timeout=300.0, headers=headers) as http,
                    http.stream(
                        "POST",
                        f"{base_url}/api/v1/sessions/{session_id}/prompt/stream",
                        json={"content": user_input, "stream": True},
                    ) as response,
                ):
                    response.raise_for_status()
                    full_response = ""

                    with Live(
                        Panel(Markdown("⏳ *Thinking...*"), border_style="cyan"),
                        console=console,
                        refresh_per_second=10,
                        transient=False,
                    ) as live:
                        try:
                            for line in response.iter_lines():
                                if not line:
                                    continue
                                try:
                                    data = json.loads(line)
                                    if data.get("type") in {"session_created", "session_switched"}:
                                        session_id = data.get("session_id", session_id)
                                        full_response = data.get("content", "")
                                        live.update(Panel(Markdown(full_response), border_style="green"))
                                    if data.get("type") == "chunk":
                                        chunk = data.get("content", "")
                                        full_response += chunk
                                        live.update(Panel(Markdown(full_response), border_style="cyan"))
                                    elif data.get("type") == "tool_call":
                                        # Show tool call indicator
                                        tool_name = data.get("tool_name", "unknown")
                                        live.update(
                                            Panel(
                                                Markdown(f"{full_response}\n\n🔧 *Calling tool: {tool_name}...*"),
                                                border_style="cyan",
                                            )
                                        )
                                    elif data.get("type") == "error":
                                        live.update(
                                            Panel(
                                                f"[red]Error: {data.get('error')}[/red]",
                                                border_style="red",
                                            )
                                        )
                                    elif data.get("type") == "complete":
                                        if full_response:
                                            live.update(Panel(Markdown(full_response), border_style="cyan"))
                                except json.JSONDecodeError:
                                    pass
                        except KeyboardInterrupt:
                            # Handle cancellation
                            live.update(
                                Panel(
                                    Markdown(f"{full_response}\n\n[yellow]Cancelling...[/yellow]"),
                                    border_style="yellow",
                                )
                            )
                            try:
                                with httpx.Client(timeout=5.0, headers=headers) as cancel_http:
                                    cancel_response = cancel_http.post(
                                        f"{base_url}/api/v1/sessions/{session_id}/cancel"
                                    )
                                    cancel_response.raise_for_status()
                                    console.print("\n[yellow]Operation cancelled.[/yellow]")
                            except Exception as e:
                                console.print(f"\n[red]Failed to send cancel request: {e}[/red]")
                            # Don't re-raise to keep the session alive
                            continue

            except Exception as e:
                console.print(f"[red]Request failed: {e}[/red]")

            console.print()

        except EOFError:
            console.print("[green]Goodbye! 👋[/green]")
            break
        except KeyboardInterrupt:
            # This handles Ctrl+C when not streaming (at the prompt)
            console.print("\n[yellow]Interrupted. Type /exit to quit.[/yellow]")
        except Exception as e:
            console.print(f"[red]Error: {e}[/red]")


@mcp.command("setup-parallel", help="Add the optional Parallel Search MCP server")
def mcp_setup_parallel() -> None:
    """Add Parallel Search MCP without replacing user-owned server configuration."""
    cfg: AnkaloopConfig = load_config()
    collision = next((name for name in ("parallel", "parallel-search") if name in cfg.servers), None)
    if collision is not None:
        raise typer.BadParameter(f"Server name {collision!r} is already configured; refusing to overwrite it.")
    cfg.servers["parallel-search"] = Server(url="https://search.parallel.ai/mcp")
    path = save_config(cfg)
    console.print(f"[green]Added Parallel Search MCP server to {path}[/green]")
    console.print(
        "[yellow]When used, its tools send search objectives, search queries, and requested URLs to Parallel.[/yellow]"
    )


@mcp.command("tools", help="List tools from a configured MCP server")
def mcp_tools(server: Annotated[str, typer.Option("--server", "-s")]):
    cfg: AnkaloopConfig = load_config()
    if server not in cfg.servers:
        raise typer.BadParameter(f"Unknown server: {server}")
    tools = asyncio.run(list_mcp_tools(cfg.servers[server]))
    console.print(JSON.from_data(tools))


@mcp.command("call", help="Call a tool on a configured MCP server")
def mcp_call(
    server: Annotated[str, typer.Option("--server", "-s")],
    tool: Annotated[str, typer.Option("--tool", "-t")],
    args: Annotated[str | None, typer.Option("--args", help="JSON-encoded arguments")] = None,
):
    cfg: AnkaloopConfig = load_config()
    if server not in cfg.servers:
        raise typer.BadParameter(f"Unknown server: {server}")
    arguments = json.loads(args) if args else {}
    resp = asyncio.run(call_mcp_tool(cfg.servers[server], tool, arguments))
    console.print(JSON.from_data(resp))


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    version: Annotated[
        bool | None,
        typer.Option(
            "--version", "-v", help="Show version and git commit hash", callback=version_callback, is_eager=True
        ),
    ] = None,
    message: Annotated[str | None, typer.Option("--once", help="Send one message and exit")] = None,
    agent_file: Annotated[str | None, typer.Option("--agent", help="Path to agent specification file")] = None,
    agent_type: Annotated[
        str | None,
        typer.Option("--agent-type", "-t", help="Built-in agent type: coder, explorer, planner, focused_coder"),
    ] = None,
    work_dir: Annotated[
        Path | None,
        typer.Option(
            "--work-dir", "-w", help="Set working directory", exists=True, file_okay=False, dir_okay=True, readable=True
        ),
    ] = None,
    no_progress: Annotated[bool, typer.Option("--no-progress", help="Disable progress indicators")] = False,
    list_agents: Annotated[bool, typer.Option("--list", help="List available agent specifications")] = False,
    list_agent_types: Annotated[bool, typer.Option("--list-types", help="List available built-in agent types")] = False,
    session_id: Annotated[
        str | None, typer.Option("--session", help="Use specific session ID for conversation continuity")
    ] = None,
    clear_session: Annotated[bool, typer.Option("--clear", help="Clear conversation history for the session")] = False,
    list_sessions: Annotated[
        bool, typer.Option("--list-sessions", help="List available conversation sessions")
    ] = False,
) -> None:
    """Enhanced agent chat with improved tool management and context awareness."""

    # If a subcommand is invoked, don't run the agent
    if ctx.invoked_subcommand is not None:
        return

    # Handle list agent types
    if list_agent_types:
        registry = get_agent_registry()
        table = Table(title="Available Built-in Agent Types")
        table.add_column("Name", style="cyan")
        table.add_column("Mode", style="magenta")
        table.add_column("Description", style="green")
        table.add_column("Can Delegate", style="yellow")
        table.add_column("Max Steps", style="blue")

        for name in registry.list_agents():
            config = registry.get(name)
            if config:
                table.add_row(
                    config.name,
                    config.mode.value,
                    config.description[:50] + "..." if len(config.description) > 50 else config.description,
                    "✅" if config.can_delegate else "❌",
                    str(config.max_steps),
                )

        console.print(table)
        console.print()
        console.print("[dim]Use --agent-type <name> or -t <name> to select an agent type[/dim]")
        console.print("[dim]Example: anka -t explorer --once 'Find all TODO comments'[/dim]")
        return

    # Handle session listing
    if list_sessions:
        sessions_dir = Path.home() / ".config" / CONFIG_DIR_NAME / "sessions"
        if not sessions_dir.exists():
            console.print("[yellow]No sessions directory found[/yellow]")
            return

        session_files = list(sessions_dir.glob("*.json"))
        if not session_files:
            console.print("[yellow]No conversation sessions found[/yellow]")
            return

        console.print("[bold]Available Conversation Sessions:[/bold]")
        for session_file in sorted(session_files, key=lambda f: f.stat().st_mtime, reverse=True):
            try:
                with open(session_file, encoding="utf-8") as f:
                    data = json.load(f)
                    conversation = data.get("conversation", {})
                    messages = conversation.get("messages", data.get("conversation_history", []))
                    console.print(f"📄 {data.get('session_id', session_file.stem)}")
                    console.print(f"   Agent: {data.get('agent_name', 'Unknown')}")
                    console.print(f"   Messages: {len(messages)}")
                    console.print(f"   Created: {data.get('created_at', 'Unknown')}")
                    console.print(f"   File: {session_file}")
                    console.print()
            except Exception as e:
                console.print(f"❌ {session_file.name}: {e}")
        return

    if list_agents:
        # Check both global config dir and local agents dir
        agents_dir = Path(os.path.expanduser(f"~/.config/{CONFIG_DIR_NAME}/agents"))
        local_agents_dir = Path("agents")

        agent_files_list = list_available_agents(agents_dir)
        local_agent_files = list_available_agents(local_agents_dir)

        # Combine both lists
        all_agent_files = agent_files_list + local_agent_files

        if not all_agent_files:
            console.print(
                "[yellow]No agent specifications found. Create one in ~/.config/ankaloop/agents/ or local agents/[/yellow]"
            )
            return

        console.print("[bold]Available Agent Specifications:[/bold]")
        for agent_file_path in all_agent_files:
            try:
                spec = load_agent_spec(agent_file_path)
                console.print(f"📄 {agent_file_path.name}")
                console.print(f"   Name: {spec.name}")
                console.print(f"   Mode: {spec.mode.value}")
                console.print(f"   Description: {spec.description}")
                console.print(f"   Tools: {'all' if spec.tools is None else len(spec.tools)}")
                console.print()
            except Exception as e:
                console.print(f"❌ {agent_file_path.name}: {e}")

        # Also show default agent
        default_spec = get_default_agent_spec()
        console.print("[bold]Default Agent:[/bold]")
        console.print(f"   Name: {default_spec.name}")
        console.print(f"   Mode: {default_spec.mode.value}")
        console.print(f"   Description: {default_spec.description}")
        console.print(f"   Tools: {'all' if default_spec.tools is None else len(default_spec.tools)}")
        return

    # Load configuration
    cfg = load_config()

    try:
        # Determine agent to use (priority: --agent > --agent-type > config.default_agent > default)
        agent = None

        if agent_file:
            # Load from YAML file
            agent_path = Path(agent_file).expanduser()
            if not agent_path.exists():
                console.print(f"[red]Agent file not found: {agent_path}[/red]")
                raise typer.Exit(1)
            agent_spec = load_agent_spec(agent_path)
            agent = Agent(agent_spec, session_id=session_id)
            console.print(f"[green]Loaded agent from file: {agent_spec.name} ({agent_spec.mode.value})[/green]")

        elif agent_type:
            # Use built-in agent type
            registry = get_agent_registry()
            if agent_type not in registry.list_agents():
                console.print(f"[red]Unknown agent type: {agent_type}[/red]")
                console.print(f"[dim]Available types: {', '.join(registry.list_agents())}[/dim]")
                raise typer.Exit(1)
            agent = create_agent_by_name(agent_type, session_id=session_id)
            console.print(f"[green]Using agent: {agent.name} ({agent.agent_spec.mode.value})[/green]")

        elif cfg.chat and cfg.chat.default_agent:
            # Use agent from config
            registry = get_agent_registry()
            if cfg.chat.default_agent in registry.list_agents():
                agent = create_agent_by_name(cfg.chat.default_agent, session_id=session_id)
                console.print(f"[green]Using configured agent: {agent.name} ({agent.agent_spec.mode.value})[/green]")
            else:
                console.print(
                    f"[yellow]Warning: Configured agent '{cfg.chat.default_agent}' not found, using default[/yellow]"
                )
                agent_spec = get_default_agent_spec()
                agent = Agent(agent_spec, session_id=session_id)

        else:
            # Use default agent
            agent_spec = get_default_agent_spec()
            agent = Agent(agent_spec, session_id=session_id)
            console.print(f"[green]Using default agent: {agent_spec.name}[/green]")

        # Handle session clearing
        if clear_session:
            asyncio.run(agent.clear_conversation_history())
            console.print(f"[green]Cleared conversation history for session: {agent.session_id}[/green]")

        # Show session info
        session_info = agent.get_conversation_summary()
        if session_info["message_count"] > 0:
            console.print(f"[dim]Session {agent.session_id}: {session_info['message_count']} messages in history[/dim]")
        else:
            console.print(f"[dim]New session started: {agent.session_id}[/dim]")

        if message is not None:
            # Single message mode
            console.print(f"[bold]🤖 Agent {agent.name}[/bold] - Processing...")
            response = asyncio.run(
                agent.services.session_service.run(
                    agent,
                    message,
                    work_dir=work_dir,
                    stream=False,
                    show_progress=not no_progress,
                )
            )

            console.print(Panel(Markdown(response), title=f"Agent {agent.name}", border_style="cyan"))

            # Show execution summary
            summary = agent.get_execution_summary()
            console.print(f"[dim]LLM Calls: {summary['llm_calls']} | Tools called: {summary['tools_called']}[/dim]")

        else:
            # Interactive mode
            console.print(f"[bold]🤖 Agent {agent.name} - Interactive Mode[/bold]")
            console.print(f"[dim]Description: {agent.agent_spec.description}[/dim]")
            console.print(
                f"[dim]Max steps: {agent.agent_spec.max_steps} | Mode: {agent.agent_spec.mode.value} | Session: {agent.session_id}[/dim]"
            )
            console.print("[dim]Commands: /help for commands, /skills for skills, /exit to quit[/dim]")

            # Initialize skills and commands
            skill_manager = get_skill_manager()
            skill_manager.discover_skills(work_dir)
            command_manager = get_command_manager()
            command_manager.discover_commands(work_dir)
            console.print()

            # Setup prompt_toolkit with history
            history_file = Path.home() / ".config" / CONFIG_DIR_NAME / "history.txt"
            history_file.parent.mkdir(parents=True, exist_ok=True)
            session: PromptSession[str] = PromptSession(history=FileHistory(str(history_file)))

            while True:
                try:
                    user_input = session.prompt("You: ").strip()

                    if not user_input:
                        continue

                    # Check for slash commands
                    if user_input.startswith("/"):
                        matched_cmd, cmd_args = command_manager.parse_input(user_input)
                        if matched_cmd:
                            result = command_manager.execute_command(
                                matched_cmd, cmd_args, work_dir=work_dir, project_root=work_dir
                            )

                            if result.type == "handled":
                                # Handle special commands
                                if result.content == "exit":
                                    console.print("[green]Goodbye! 👋[/green]")
                                    break
                                elif result.content == "clear":
                                    asyncio.run(agent.clear_conversation_history())
                                    console.print(
                                        f"[green]Conversation history cleared for session: {agent.session_id}[/green]"
                                    )
                                elif result.content == "info":
                                    session_info = agent.get_conversation_summary()
                                    console.print("[bold]Session Info:[/bold]")
                                    console.print(f"Session ID: {session_info['session_id']}")
                                    console.print(f"Agent: {session_info['agent_name']}")
                                    console.print(f"Messages: {session_info['message_count']}")
                                    console.print(f"Total LLM Calls: {session_info['total_llm_calls']}")
                                    console.print(f"Total Tool Calls: {session_info['total_tool_calls']}")
                                    console.print(f"Session file: {session_info['session_file']}")
                                    # Show active skills
                                    active_skills = skill_manager.get_active_skills()
                                    if active_skills:
                                        console.print(f"Active skills: {', '.join(s.name for s in active_skills)}")
                                elif result.content == "new_session":
                                    agent = Agent(agent.agent_spec)
                                    console.print(f"[green]New session started: {agent.session_id}[/green]")
                                elif result.content == "session:list":
                                    session_info = agent.get_conversation_summary()
                                    console.print("[bold]Sessions:[/bold]")
                                    console.print(f"* {session_info['session_id']} (current)")
                                elif result.content.startswith("session:switch "):
                                    target_session_id = result.content.removeprefix("session:switch ").strip()
                                    agent = Agent(agent.agent_spec, session_id=target_session_id)
                                    console.print(f"[green]Switched to session: {agent.session_id}[/green]")
                                elif result.content == "cancel":
                                    console.print("[yellow]No active request to cancel.[/yellow]")
                                console.print()
                                continue

                            elif result.type == "message":
                                # Display message
                                if result.message_type == "error":
                                    console.print(f"[red]{result.content}[/red]")
                                elif result.message_type == "success":
                                    console.print(f"[green]{result.content}[/green]")
                                else:
                                    console.print(Panel(Markdown(result.content), border_style="dim"))
                                console.print()
                                continue

                            elif result.type == "submit_prompt":
                                # Submit the processed prompt to the agent
                                user_input = result.content
                                # Fall through to agent processing
                        else:
                            console.print(f"[yellow]Unknown command: {user_input}[/yellow]")
                            console.print("[dim]Use /help to see available commands[/dim]")
                            console.print()
                            continue

                    # Check for legacy commands (for backward compatibility)
                    if user_input.lower() in ["exit", "quit", "q"]:
                        console.print("[green]Goodbye! 👋[/green]")
                        break

                    if user_input.lower() == "clear":
                        asyncio.run(agent.clear_conversation_history())
                        console.print(f"[green]Conversation history cleared for session: {agent.session_id}[/green]")
                        continue

                    if user_input.lower() == "info":
                        session_info = agent.get_conversation_summary()
                        console.print("[bold]Session Info:[/bold]")
                        console.print(f"Session ID: {session_info['session_id']}")
                        console.print(f"Agent: {session_info['agent_name']}")
                        console.print(f"Messages: {session_info['message_count']}")
                        console.print(f"Total LLM Calls: {session_info['total_llm_calls']}")
                        console.print(f"Total Tool Calls: {session_info['total_tool_calls']}")
                        console.print(f"Session file: {session_info['session_file']}")
                        console.print()
                        continue

                    console.print(f"[bold]🤖 Agent {agent.name}[/bold] - Processing...")
                    response = asyncio.run(
                        agent.services.session_service.run(
                            agent,
                            user_input,
                            work_dir=work_dir,
                            stream=False,
                            show_progress=not no_progress,
                        )
                    )

                    console.print(Panel(Markdown(response), title=f"Agent {agent.name}", border_style="cyan"))

                    # Show execution summary
                    summary = agent.get_execution_summary()
                    console.print(
                        f"[dim]LLM Calls: {summary['llm_calls']} | Tools called: {summary['tools_called']} | Session: {agent.session_id}[/dim]"
                    )
                    console.print()

                except EOFError:
                    console.print("[green]Goodbye! 👋[/green]")
                    break
                except KeyboardInterrupt:
                    console.print("\\n[yellow]Interrupted. Type /exit to quit.[/yellow]")
                except Exception as e:
                    console.print(f"[red]Error: {e}[/red]")
                    console.print()

    except Exception as e:
        console.print(f"[red]Agent failed:[/red] {e}")
        raise typer.Exit(code=1) from None


@app.command(help="Enhanced agent chat (alias for default command)")
def agent(
    ctx: typer.Context,
    message: Annotated[str | None, typer.Option("--once", help="Send one message and exit")] = None,
    agent_file: Annotated[str | None, typer.Option("--agent", help="Path to agent specification file")] = None,
    agent_type: Annotated[
        str | None,
        typer.Option("--agent-type", "-t", help="Built-in agent type: coder, explorer, planner, focused_coder"),
    ] = None,
    work_dir: Annotated[
        Path | None,
        typer.Option(
            "--work-dir", "-w", help="Set working directory", exists=True, file_okay=False, dir_okay=True, readable=True
        ),
    ] = None,
    no_progress: Annotated[bool, typer.Option("--no-progress", help="Disable progress indicators")] = False,
    list_agents: Annotated[bool, typer.Option("--list", help="List available agent specifications")] = False,
    list_agent_types: Annotated[bool, typer.Option("--list-types", help="List available built-in agent types")] = False,
    session_id: Annotated[
        str | None, typer.Option("--session", help="Use specific session ID for conversation continuity")
    ] = None,
    clear_session: Annotated[bool, typer.Option("--clear", help="Clear conversation history for the session")] = False,
    list_sessions: Annotated[
        bool, typer.Option("--list-sessions", help="List available conversation sessions")
    ] = False,
) -> None:
    """Enhanced agent chat (alias for default command)."""
    ctx.invoke(
        main,
        message=message,
        agent_file=agent_file,
        agent_type=agent_type,
        work_dir=work_dir,
        no_progress=no_progress,
        list_agents=list_agents,
        list_agent_types=list_agent_types,
        session_id=session_id,
        clear_session=clear_session,
        list_sessions=list_sessions,
    )


if __name__ == "__main__":
    app()
