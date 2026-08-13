"""
usbmuxd-bridge CLI entry point.

Usage:
    usbmuxd-bridge --agent-host 192.168.1.10 --token SECRET [OPTIONS]

After starting, set the env var in your diagnostic terminal:
    export USBMUXD_SOCKET_ADDRESS=unix:/tmp/usbmuxd.sock

Then run iScan normally:
    iscan report --open
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel

app = typer.Typer(
    name="usbmuxd-bridge",
    help="NetworkUSB Bridge — runs on the master Mac with the diagnostic software.",
    add_completion=False,
    pretty_exceptions_enable=False,
)
console = Console()
logger = logging.getLogger(__name__)


@app.command()
def main(
    agent_host: str = typer.Option(
        ..., "--agent-host", help="Hostname or IP of the usbmuxd-agent"
    ),
    agent_port: int = typer.Option(
        8721, "--agent-port", show_default=True, help="TCP port of the usbmuxd-agent"
    ),
    token: str | None = typer.Option(
        None,
        "--token",
        envvar="USBMUXD_TOKEN",
        help="Shared secret — must match agent's --token",
    ),
    token_file: Path | None = typer.Option(
        None,
        "--token-file",
        help="File containing shared secret token",
    ),
    expected_fingerprint: str | None = typer.Option(
        None,
        "--expected-fingerprint",
        help="SHA-256 TLS fingerprint of expected agent certificate",
    ),
    socket_path: str = typer.Option(
        "/tmp/usbmuxd.sock",
        "--socket-path",
        show_default=True,
        help="Local UNIX socket path exposed to libimobiledevice",
    ),
    socket_mode: str = typer.Option(
        "0700",
        "--socket-mode",
        show_default=True,
        help="Permissions mode for local UNIX socket file (e.g. 0700 or 0777)",
    ),
    log_level: str = typer.Option(
        "INFO",
        "--log-level",
        show_default=True,
        help="Logging verbosity: DEBUG | INFO | WARNING | ERROR",
    ),
) -> None:
    """Start the usbmuxd-bridge and connect to the remote agent."""
    from networkusb.bridge.client import BridgeClient
    from networkusb.tls import make_client_ssl_context
    from networkusb.utils import resolve_token, setup_logging

    setup_logging(log_level)

    try:
        secret_token = resolve_token(token, token_file)
    except ValueError as exc:
        console.print(f"[bold red]Configuration error:[/bold red] {exc}")
        raise typer.Exit(1)

    try:
        parsed_mode = int(socket_mode, 8)
    except ValueError:
        console.print(f"[bold red]Invalid socket mode:[/bold red] {socket_mode}")
        raise typer.Exit(1)

    ssl_ctx = make_client_ssl_context()

    # ---- Startup banner ----
    console.print(
        Panel(
            f"[bold cyan]usbmuxd-bridge[/bold cyan] v{_get_version()}\n\n"
            f"[cyan]Agent:[/cyan]  [white]{agent_host}:{agent_port}[/white]\n"
            f"[cyan]Socket:[/cyan] [white]{socket_path}[/white] (mode {oct(parsed_mode)})\n\n"
            "[bold yellow]Run this in your diagnostic terminal:[/bold yellow]\n"
            f"[bold white]export USBMUXD_SOCKET_ADDRESS={socket_path}[/bold white]\n\n"
            "[dim]Bridge will reconnect automatically if the agent is temporarily unavailable.[/dim]",
            title="[cyan]🌉 NetworkUSB Bridge[/cyan]",
            border_style="cyan",
            padding=(1, 2),
        )
    )

    client = BridgeClient(
        agent_host=agent_host,
        agent_port=agent_port,
        token=secret_token,
        socket_path=socket_path,
        ssl_context=ssl_ctx,
        expected_fingerprint=expected_fingerprint,
        socket_mode=parsed_mode,
    )

    try:
        asyncio.run(client.run())
    except KeyboardInterrupt:
        console.print("\n[yellow]Bridge stopped.[/yellow]")
    except Exception as exc:
        logger.critical("Bridge crashed: %s", exc, exc_info=True)
        raise typer.Exit(2)



def _get_version() -> str:
    try:
        from networkusb import __version__
        return __version__
    except Exception:
        return "unknown"
