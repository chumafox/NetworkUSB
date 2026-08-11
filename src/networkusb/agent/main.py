"""
usbmuxd-agent CLI entry point.

Usage:
    usbmuxd-agent --token SECRET [OPTIONS]

    # Minimum (uses defaults for everything else):
    sudo usbmuxd-agent --token mysecret

    # Explicit:
    sudo usbmuxd-agent \\
        --host 0.0.0.0 \\
        --port 8721 \\
        --token mysecret \\
        --usbmuxd-path /var/run/usbmuxd \\
        --cert-dir ~/.config/usbmuxd-agent \\
        --log-level INFO \\
        --foreground
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel

app = typer.Typer(
    name="usbmuxd-agent",
    help="NetworkUSB Agent — runs on the Mac with the iPhone attached.",
    add_completion=False,
    pretty_exceptions_enable=False,
)
console = Console(stderr=True)
logger = logging.getLogger(__name__)

_DEFAULT_CERT_DIR = Path.home() / ".config" / "usbmuxd-agent"


@app.command()
def main(
    host: str = typer.Option(
        "0.0.0.0", "--host", show_default=True, help="Host / IP to listen on"
    ),
    port: int = typer.Option(
        8721, "--port", show_default=True, help="TCP port to listen on"
    ),
    token: str = typer.Option(
        "",
        "--token",
        envvar="USBMUXD_TOKEN",
        help="Shared secret used to authenticate bridge connections",
    ),
    token_file: Path | None = typer.Option(
        None,
        "--token-file",
        help="Read the shared secret from this file (root-owned 0600). "
        "Takes precedence over --token/USBMUXD_TOKEN; recommended for "
        "LaunchDaemon deployment.",
    ),
    usbmuxd_path: str = typer.Option(
        "/var/run/usbmuxd",
        "--usbmuxd-path",
        show_default=True,
        help="Path to the local usbmuxd UNIX socket",
    ),
    cert_dir: Path = typer.Option(
        _DEFAULT_CERT_DIR,
        "--cert-dir",
        show_default=True,
        help="Directory that stores the TLS certificate and private key",
    ),
    log_level: str = typer.Option(
        "INFO",
        "--log-level",
        show_default=True,
        help="Logging verbosity: DEBUG | INFO | WARNING | ERROR",
    ),
    foreground: bool = typer.Option(
        False,
        "--foreground",
        "-f",
        help="Run in foreground (skip file logging; useful for debugging)",
    ),
) -> None:
    """Start the usbmuxd-agent server."""
    from networkusb.utils import setup_logging, check_unix_socket_accessible
    from networkusb.tls import generate_self_signed, get_fingerprint, make_server_ssl_context
    from networkusb.agent.server import AgentServer

    # ---- Logging ----
    log_file: Path | None = None
    if not foreground:
        log_file = Path("/var/log/usbmuxd-agent.log")
    setup_logging(log_level, log_file)

    # ---- Validate usbmuxd socket ----
    if not os.path.exists(usbmuxd_path):
        console.print(
            Panel(
                f"[bold red]Socket not found:[/bold red] [yellow]{usbmuxd_path}[/yellow]\n\n"
                "Possible causes:\n"
                "  • usbmuxd is not running — connect an iPhone and try again\n"
                "  • Insufficient permissions — try: [bold]sudo usbmuxd-agent ...[/bold]\n"
                "  • Custom path — use [bold]--usbmuxd-path[/bold] to specify it",
                title="[red]❌ Cannot access usbmuxd[/red]",
                border_style="red",
            )
        )
        raise typer.Exit(1)

    # ---- TLS certificate ----
    try:
        cert_path, key_path = generate_self_signed(cert_dir.expanduser())
        fingerprint = get_fingerprint(cert_path)
        ssl_ctx = make_server_ssl_context(cert_path, key_path)
    except Exception as exc:
        console.print(f"[bold red]TLS setup failed:[/bold red] {exc}")
        raise typer.Exit(1)

    # ---- Startup banner ----
    console.print(
        Panel(
            f"[bold green]usbmuxd-agent[/bold green] v{_get_version()}\n\n"
            f"[cyan]Listen:[/cyan]   [white]{host}:{port}[/white]\n"
            f"[cyan]usbmuxd:[/cyan]  [white]{usbmuxd_path}[/white]\n"
            f"[cyan]Cert:[/cyan]     [white]{cert_path}[/white]\n\n"
            f"[bold yellow]TLS Fingerprint (SHA-256):[/bold yellow]\n"
            f"[white]{fingerprint}[/white]\n\n"
            "[dim]Share this fingerprint with the bridge operator.\n"
            "The bridge will pin it on first connection.[/dim]",
            title="[green]🔌 NetworkUSB Agent[/green]",
            border_style="green",
            padding=(1, 2),
        )
    )

    # ---- Resolve shared secret (--token-file > --token / USBMUXD_TOKEN) ----
    if token_file is not None:
        token_file = token_file.expanduser()
        if not token_file.is_file():
            console.print(f"[bold red]Token file not found:[/bold red] {token_file}")
            raise typer.Exit(1)
        resolved_token = token_file.read_text(encoding="utf-8").strip()
        if not resolved_token:
            console.print(f"[bold red]Token file is empty:[/bold red] {token_file}")
            raise typer.Exit(1)
    else:
        resolved_token = token
    if not resolved_token:
        console.print(
            "[bold red]No token provided.[/bold red]\n"
            "Pass --token, set USBMUXD_TOKEN, or use --token-file."
        )
        raise typer.Exit(1)

    server = AgentServer(
        host=host,
        port=port,
        token=resolved_token,
        usbmuxd_path=usbmuxd_path,
        ssl_context=ssl_ctx,
    )

    try:
        asyncio.run(server.start())
    except KeyboardInterrupt:
        console.print("\n[yellow]Agent stopped.[/yellow]")
    except Exception as exc:
        logger.critical("Agent crashed: %s", exc, exc_info=True)
        raise typer.Exit(2)


def _get_version() -> str:
    try:
        from networkusb import __version__
        return __version__
    except Exception:
        return "unknown"
