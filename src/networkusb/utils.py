"""Shared utilities: logging setup, TCP keepalive, UNIX socket check."""

from __future__ import annotations

import logging
import socket
import sys
from collections.abc import AsyncIterator
from logging.handlers import RotatingFileHandler
from pathlib import Path


def setup_logging(level: str, log_file: Path | None = None) -> None:
    """
    Configure the root logger.

    Args:
        level:    One of DEBUG / INFO / WARNING / ERROR (case-insensitive).
        log_file: Optional path for file logging with rotation (10 MB × 3 backups).
                  When None, only stderr is used.
    """
    numeric_level = getattr(logging, level.upper(), logging.INFO)

    fmt = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=10 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setFormatter(logging.Formatter(fmt, datefmt=datefmt))
        handlers.append(file_handler)

    logging.basicConfig(
        level=numeric_level,
        format=fmt,
        datefmt=datefmt,
        handlers=handlers,
        force=True,
    )

    # Silence overly verbose third-party loggers
    logging.getLogger("asyncio").setLevel(logging.WARNING)


def check_unix_socket_accessible(path: str) -> bool:
    """
    Return True if *path* exists as a UNIX socket and is connectable.

    Performs a brief connect attempt; does not require usbmuxd to accept
    the connection (just checks the socket is reachable).
    """
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(2.0)
    try:
        sock.connect(path)
        return True
    except (FileNotFoundError, ConnectionRefusedError, PermissionError, OSError):
        return False
    finally:
        sock.close()


def apply_tcp_keepalive(sock: socket.socket) -> None:
    """
    Enable TCP keepalive on *sock* with sensible defaults.

    Keepalive probes start after 60 s of idle, sent every 10 s,
    connection dropped after 3 missed probes.

    Note: TCP_KEEPIDLE / TCP_KEEPINTVL / TCP_KEEPCNT are Linux + macOS 10.8+.
    """
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    if hasattr(socket, "TCP_KEEPIDLE"):
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 60)
    if hasattr(socket, "TCP_KEEPINTVL"):
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10)
    if hasattr(socket, "TCP_KEEPCNT"):
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)


async def exponential_backoff(
    initial: float = 1.0, maximum: float = 30.0
) -> AsyncIterator[float]:
    """
    Async generator that yields exponential backoff delays.

    Yields: 1, 2, 4, 8, 16, 30, 30, 30, ...
    """
    import asyncio

    delay = initial
    while True:
        yield delay
        await asyncio.sleep(delay)
        delay = min(delay * 2, maximum)


def resolve_token(token: str | None, token_file: Path | None) -> str:
    """
    Resolve authentication token from either a string or a file path.

    Raises ValueError if neither or both are supplied, or if the file is unreadable.
    """
    if token and token_file:
        raise ValueError("Specify either --token or --token-file, not both.")

    if token_file:
        token_file = token_file.expanduser()
        if not token_file.is_file():
            raise ValueError(f"Token file not found: {token_file}")
        st_mode = token_file.stat().st_mode
        if st_mode & 0o077:
            logging.warning(
                "Token file %s permissions (%o) are too permissive (recommended 0600)",
                token_file,
                st_mode & 0o777,
            )
        resolved = token_file.read_text(encoding="utf-8").strip()
        if not resolved:
            raise ValueError(f"Token file is empty: {token_file}")
        return resolved

    if token:
        resolved = token.strip()
        if not resolved:
            raise ValueError("Token cannot be empty.")
        return resolved

    raise ValueError("Either --token or --token-file must be provided.")

