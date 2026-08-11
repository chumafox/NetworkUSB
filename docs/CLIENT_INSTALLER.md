# Клиентский инсталлер (.pkg)

Один файл, который клиент ставит двойным кликом. Всё остальное происходит само:
проверка зависимостей, Tailscale, Python, агент, LaunchDaemon. После установки
машина клиента появляется в твоём tailnet — и ты подключаешься к её iPhone
как к локальному.

## Как это работает

```
build_client_pkg.sh  ──►  NetworkUSB-Client-<дата>.pkg  ──►  двойной клик у клиента
   (машина мастера)                                            │
                                                               ▼
                                                    postinstall (root):
                                                    0. PREFLIGHT — проверка
                                                       зависимостей; любой
                                                       [FAIL] → отмена
                                                    1. Tailscale (если нет)
                                                    2. tailscale up --authkey
                                                    3. uv → Python 3.14 → venv
                                                    4. pip install -e .
                                                    5. секрет в /etc/networkusb
                                                    6. LaunchDaemon :8721
```

## Сборка (машина мастера)

Нужно: Xcode Command Line Tools, curl, интернет, tailnet в Tailscale.

1. Создай одноразовый auth key:
   https://login.tailscale.com/admin/settings/keys → «Generate auth key» →
   одиночное использование, срок 1 час.

2. Собери пакет:

   cd ~/Projects/NetworkUSB
   TS_AUTHKEY=tskey-auth-XXXXXXXX ./installer/build_client_pkg.sh \
       --hostname magazin-01

   Результат: dist/NetworkUSB-Client-<дата>.pkg (~60 МБ).

Альтернатива — CI: Actions → «Build client installer» → Run workflow
(secret TS_AUTHKEY в Settings → Secrets). Артефакт скачаешь с вкладки
Summary → Artifacts.

## Установка (машина клиента)

1. Положи пакет на мак клиента (AirDrop / scp / облако).
2. Клиент делает двойной клик по .pkg → Installer → пароль администратора.
3. Если macOS спросит «Tailscale хочет добавить VPN-конфигурации» — Allow.
4. Если появится предупреждение «unidentified developer» — правый клик по
   файлу → «Открыть» (или System Settings → Privacy & Security → Open Anyway).
5. Ждать 2–5 минут (ставится Python). В конце — «Installation Succeeded».

Если пакет не подписан — возможен вариант установки по SSH:

   scp NetworkUSB-Client-<дата>.pkg клиент:/tmp/
   ssh клиент 'sudo installer -pkg /tmp/NetworkUSB-Client-<дата>.pkg -target /'

## Проверки перед установкой (PREFLIGHT)

Инсталлер НЕ ставит ничего, пока не пройдены все проверки; при любом [FAIL]
установка отменяется с логом /var/log/networkusb-install.log:

| Проверка                 | Зачем |
|--------------------------|-------|
| macOS 13+                | Tailscale и наши бинари |
| Архитектура arm64/x86_64 | поддерживаемые платформы |
| Свободное место ≥ 2 ГБ   | Python + venv + код |
| Интернет (pypi.org)      | pip install |
| usbmuxd запущен          | сервис должен работать |
| iPhone по USB (ioreg)    | без устройства агент бесполезен — подключи и нажми «Доверять» |

## Что делать мастеру после установки

1. Найди машину в Tailscale: https://login.tailscale.com/admin/machines
   (имя из --hostname) — там же её Tailscale IP (100.x.y.z).
2. Прочитай общий секрет с машины клиента:
   ssh клиент 'sudo cat /etc/networkusb/token'
3. Пропиши в ~/.config/usbmuxd-bridge/config.json на своей машине:
   agent_host = 100.x.y.z, token = <секрет>.
4. Перезапусти bridge (меню-бар NetworkUSB → Stop → Start) и проверь:
   USBMUXD_SOCKET_ADDRESS=/tmp/usbmuxd.sock \
     ~/Projects/iScan/.venv/bin/pymobiledevice3 usbmux list

## Диагностика

- Лог установки на машине клиента:  /var/log/networkusb-install.log
- Лог агента:                       /var/log/usbmuxd-agent.log
- Сводка (root-only):               /etc/networkusb/INSTALL_SUMMARY.txt
  (Tailscale IP, hostname, состояние агента)
- Агент слушает :8721:              lsof -nP -iTCP:8721 -sTCP:LISTEN
- Переустановка: двойной клик по пакету снова — токен и сертификаты
  сохраняются (не перезаписываются), код обновляется.

## Безопасность

- Tailscale auth key — одноразовый, сгорает при первой установке; создавай
  новый на каждый пакет, срок 1 час.
- Общий секрет агента генерируется на машине клиента при установке — в пакет
  не попадает, в plist не пишется (--token-file, root:wheel 600).
- Трафик: WireGuard (Tailscale) + TLS 1.2+ (наш протокол) + pinning
  (known_hosts) + токен-аутентификация.
- Пакет не содержит ключей мастера — только одноразовый auth key.
