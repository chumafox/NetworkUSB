#!/bin/bash
# =============================================================================
# NetworkUSB — сборка клиентского инсталлера (.pkg)
#
# Что делает: скачивает Tailscale.pkg + uv (обе архитектуры), пакует код
# NetworkUSB и postinstall-скрипт в один .pkg. Клиент двойным кликом ставит
# всё (Tailscale + Python + агент + LaunchDaemon) и машина появляется в твоём
# tailnet. Токен генерируется НА машине клиента при установке — в пакете его
# нет. В пакете есть одноразовый Tailscale auth key (одно использование).
#
# Использование (запускать на машине мастера):
#   TS_AUTHKEY=tskey-auth-XXXX ./build_client_pkg.sh [--hostname NAME] [--out PATH]
#
# Требования: Xcode Command Line Tools (pkgbuild), curl, интернет.
# =============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOSTNAME=""
OUT=""
AUTHKEY="${TS_AUTHKEY:-}"

# --- аргументы ---------------------------------------------------------------
while [ $# -gt 0 ]; do
    case "$1" in
        --hostname) HOSTNAME="${2:-}"; shift 2 ;;
        --out)      OUT="${2:-}"; shift 2 ;;
        -h|--help)
            echo "Usage: TS_AUTHKEY=tskey-auth-... $0 [--hostname NAME] [--out PATH]"
            exit 0 ;;
        *) echo "Unknown arg: $1"; exit 1 ;;
    esac
done

if [ -z "$AUTHKEY" ]; then
    echo "ERROR: задай TS_AUTHKEY (одноразовый Tailscale auth key из admin console)." >&2
    echo "       https://login.tailscale.com/admin/settings/keys — Generate auth key," >&2
    echo "       одиночное использование, срок 1 час." >&2
    exit 1
fi
case "$AUTHKEY" in
    tskey-auth-*) ;;
    *) echo "WARNING: значение не похоже на tskey-auth-... — проверь TS_AUTHKEY" >&2 ;;
esac

command -v pkgbuild >/dev/null || { echo "ERROR: pkgbuild не найден (нужны Xcode CLT)"; exit 1; }

STAGE="$(mktemp -d /tmp/nusb-pkg.XXXXXX)"
trap 'rm -rf "$STAGE"' EXIT

echo "==> 1/6 Tailscale: headless tailscaled (userspace, без GUI)..."
# macOS Tailscale .app/.pkg тянет Network Extension и GUI-запрос «VPN-конфигурация».
# Вместо него — собранный из исходников tailscaled в userspace-режиме: без окон.
TSCALE_SRC="$ROOT/installer/third_party/tailscaled"
if [ ! -x "$TSCALE_SRC/arm64/tailscaled" ] || [ ! -x "$TSCALE_SRC/amd64/tailscaled" ]; then
    echo "ERROR: бинари tailscaled не найдены в installer/third_party/tailscaled" >&2
    exit 1
fi
echo "    tailscaled arm64 + amd64 на месте (v1.102.2)"

echo "==> 2/6 uv (aarch64 + x86_64) — из third_party/uv или качаем...)"
UV_SRC="$ROOT/installer/third_party/uv"
for ARCH in aarch64 x86_64; do
    TGZ="$UV_SRC/uv-$ARCH-apple-darwin.tar.gz"
    if [ -s "$TGZ" ]; then
        echo "    $ARCH: вендоренный $TGZ"
        cp "$TGZ" "$STAGE/"
    else
        ok=0
        for i in 1 2 3 4 5; do
            if curl -sSL --retry 2 --retry-all-errors --max-time 120 \
                -o "$STAGE/uv-$ARCH-apple-darwin.tar.gz" \
                "https://github.com/astral-sh/uv/releases/latest/download/uv-$ARCH-apple-darwin.tar.gz" \
                && [ -s "$STAGE/uv-$ARCH-apple-darwin.tar.gz" ]; then
                ok=1; break
            fi
            echo "    повтор ($i) для $ARCH..."; sleep 3
        done
        [ "$ok" = 1 ] || { echo "ERROR: не удалось получить uv ($ARCH). Положи тарбол в $UV_SRC/uv-$ARCH-apple-darwin.tar.gz или исправь сеть." >&2; exit 1; }
    fi
done

echo "==> 3/6 Сборка payload (/opt/networkusb)..."
export COPYFILE_DISABLE=1   # не тащить ._* AppleDouble из tar
PAYLOAD="$STAGE/root/opt/networkusb"
mkdir -p "$PAYLOAD/installer"
tar -C "$ROOT" \
    --exclude=.venv --exclude=.git --exclude=.gitignore \
    --exclude='._*' --exclude=.pytest_cache \
    --exclude=tests --exclude=research --exclude=dist \
    --exclude=installer --exclude=.github --exclude=docs \
    -cf - . | tar -C "$PAYLOAD" -xf -
cp -R "$TSCALE_SRC" "$PAYLOAD/installer/tailscaled"
cp "$STAGE/uv-aarch64-apple-darwin.tar.gz" "$PAYLOAD/installer/"
cp "$STAGE/uv-x86_64-apple-darwin.tar.gz"  "$PAYLOAD/installer/"
# xattr у файлов из ~/Projects заставляют pkgbuild класть ._ AppleDouble
# в Payload — зачищаем, чтобы архив был чистым
xattr -cr "$PAYLOAD" 2>/dev/null || true

echo "==> 4/6 Генерация postinstall (с auth key)..."
mkdir -p "$STAGE/scripts"
# POSTINSTALL_SRC позволяет собрать тестовый вариант (напр. без device-проверок)
POSTINSTALL_SRC="${POSTINSTALL_SRC:-$ROOT/installer/resources/postinstall}"
sed -e "s|__TS_AUTHKEY__|$AUTHKEY|" \
    -e "s|__TS_HOSTNAME__|$HOSTNAME|" \
    "$POSTINSTALL_SRC" > "$STAGE/scripts/postinstall"
chmod 755 "$STAGE/scripts/postinstall"
bash -n "$STAGE/scripts/postinstall" || { echo "ERROR: postinstall синтаксис"; exit 1; }

echo "==> 5/6 pkgbuild..."
mkdir -p "$ROOT/dist"
PKG="${OUT:-$ROOT/dist/NetworkUSB-Client-$(date +%Y%m%d).pkg}"
pkgbuild --root "$STAGE/root" --scripts "$STAGE/scripts" \
    --identifier com.networkusb.client-installer \
    --version 1.0.0 --install-location / "$PKG"
# Примечание: adhoc-подпись ("-") pkgbuild/productsign на текущих macOS не
# поддерживается; для снижения трения нужен Developer ID + notarization.
SIGNED="(unsigned)"

echo "==> 6/6 Готово $SIGNED"
ls -lh "$PKG"
echo
echo "Дальше:"
echo "  1. Передай $PKG клиенту (AirDrop / scp / облако)."
echo "  2. Клиент: двойной клик -> пароль администратора."
echo "     (Tailscale поднимается втихую: headless tailscaled, без окон/онбординга)"
echo "  3. Машина появится в твоём tailnet (admin console -> Machines)."
echo "     IP: tailscale ip -4 на машине клиента или в admin console."
echo "  4. Токен (общий секрет) читай с машины клиента:"
echo "     ssh клиент 'sudo cat /etc/networkusb/token'"
echo "  5. Впиши IP и токен в ~/.config/usbmuxd-bridge/config.json и перезапусти bridge."
