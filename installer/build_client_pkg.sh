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

echo "==> 1/6 Резолв Tailscale pkg (stable)..."
TS_FILE="$(curl -s --max-time 30 https://pkgs.tailscale.com/stable/ \
    | grep -oE 'Tailscale-[0-9][0-9.]*-macos\.pkg' | sort -Vu | tail -1)"
[ -n "$TS_FILE" ] || { echo "ERROR: не смог найти Tailscale pkg в листинге"; exit 1; }
echo "    $TS_FILE"
curl -sSL --max-time 300 -o "$STAGE/tailscale.pkg" "https://pkgs.tailscale.com/stable/$TS_FILE"
curl -sSL --max-time 30  -o "$STAGE/tailscale.pkg.sha256" "https://pkgs.tailscale.com/stable/$TS_FILE.sha256"
# .sha256 у Tailscale содержит только хеш (без имени файла) — сравниваем вручную
EXPECTED="$(tr -d '[:space:]' < "$STAGE/tailscale.pkg.sha256")"
ACTUAL="$(shasum -a 256 "$STAGE/tailscale.pkg" | awk '{print $1}')"
if [ "$EXPECTED" != "$ACTUAL" ]; then
    echo "ERROR: checksum Tailscale pkg не сошёлся" >&2
    echo "  expected: $EXPECTED" >&2
    echo "  actual:   $ACTUAL" >&2
    exit 1
fi
echo "    checksum OK"

echo "==> 2/6 Скачивание uv (aarch64 + x86_64)..."
for ARCH in aarch64 x86_64; do
    curl -sSL --max-time 120 -o "$STAGE/uv-$ARCH-apple-darwin.tar.gz" \
        "https://github.com/astral-sh/uv/releases/latest/download/uv-$ARCH-apple-darwin.tar.gz"
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
cp "$STAGE/tailscale.pkg"            "$PAYLOAD/installer/"
cp "$STAGE/uv-aarch64-apple-darwin.tar.gz" "$PAYLOAD/installer/"
cp "$STAGE/uv-x86_64-apple-darwin.tar.gz"  "$PAYLOAD/installer/"
# xattr у файлов из ~/Projects заставляют pkgbuild класть ._ AppleDouble
# в Payload — зачищаем, чтобы архив был чистым
xattr -cr "$PAYLOAD" 2>/dev/null || true

echo "==> 4/6 Генерация postinstall (с auth key)..."
mkdir -p "$STAGE/scripts"
sed -e "s|__TS_AUTHKEY__|$AUTHKEY|" \
    -e "s|__TS_HOSTNAME__|$HOSTNAME|" \
    "$ROOT/installer/resources/postinstall" > "$STAGE/scripts/postinstall"
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
echo "  2. Клиент: двойной клик -> пароль администратора -> при запросе"
echo "     'Tailscale хочет добавить VPN-конфигурации' нажать Allow."
echo "  3. Машина появится в твоём tailnet (admin console -> Machines)."
echo "     IP: tailscale ip -4 на машине клиента или в admin console."
echo "  4. Токен (общий секрет) читай с машины клиента:"
echo "     ssh клиент 'sudo cat /etc/networkusb/token'"
echo "  5. Впиши IP и токен в ~/.config/usbmuxd-bridge/config.json и перезапусти bridge."
