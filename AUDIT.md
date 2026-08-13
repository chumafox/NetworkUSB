# NetworkUSB + iScan — глубокий аудит и план улучшений

> Дата: 2026-08-13
> Репозиторий: `chumafox/NetworkUSB` @ `06fe265` (ветка аудита: `arena/019ff90d-networkusb`)
> Сопутствующий проект: `chumafox/iScan` — **исходники недоступны** (репозиторий не резолвится по GitHub API / clone; в README раньше был локальный путь `../iScan`)
> Объём NetworkUSB: ~3.1k LOC (Python 1.5k + тесты 0.7k + Swift 0.3k + installer/scripts 0.6k)
> Предыдущий внутренний разбор: `PROBLEMS.md` (F-01…F-12). Этот документ — системный аудит поверх него: продукт, безопасность, интеграция с iScan, инсталлер, UX, производительность и дорожная карта.

---

## 1. Резюме для владельца

NetworkUSB решает правильную боль: iScan / `pymobiledevice3` умеют говорить только с локальным `/var/run/usbmuxd`, а iPhone физически в магазине. Туннель UNIX-сокета по одному TLS-соединению с мультиплексом сессий — верная архитектура, уже проверенная на железе (iPhone 12 mini, агент на Mac Pro, bridge на Mac Air, `iscan report` проходит).

Проект выглядит как **рабочий прототип, который уже таскают в прод**, а не как законченный продукт для сети магазинов.

| Оценка | Балл | Комментарий |
|--------|------|-------------|
| Архитектура ядра | 8/10 | Простой бинарный mux, asyncio, разделение agent/bridge |
| Корректность протокола | 5/10 | Нет версионирования, слабая валидация, HOL между сессиями |
| Безопасность | 4/10 | TLS есть, но TOFU+токен в CLI/`ps`/`config.json`, сокет `0777` в `/tmp` |
| Надёжность | 5/10 | Реконнект есть, но сокет пропадает, heartbeat односторонний, backoff не сбрасывается |
| Операционка / инсталлер | 5/10 | `.pkg` + Tailscale — сильная идея; CI сломан, нет подписи, нет multi-shop |
| Интеграция с iScan | 4/10 | Держится на env var и парсинге строки `Report saved:` |
| Тесты / CI | 5/10 | 35 тестов ядра, нет CI на PR, нет тестов на HOL/auth/menubar/installer |
| Готовность к сети магазинов | 4/10 | Один агент, один bridge, ручной обмен IP+токеном по SSH |

**Главный риск продукта:** отчёт iScan открывает *несколько* параллельных usbmux-сессий (list / lockdown / diagnostics / AFC…). Одна медленная сессия через общий `drain()` + `_write_lock` стопорит остальные, heartbeat и опрос меню-бара. На LAN это незаметно; на Tailscale (особенно userspace) — «туннель жив, отчёт завис».

**Главный риск безопасности:** любой локальный пользователь на мастере получает полный доступ к удалённому iPhone (`/tmp/usbmuxd.sock` mode `0777`). Токен виден в `ps` и лежит в plaintext `config.json`. Первое подключение не защищено от MITM (TOFU).

**Главный операционный риск:** чтобы подключить магазин, мастер вручную достаёт Tailscale IP и `sudo cat /etc/networkusb/token`. На 1–2 точках терпимо. На 10+ — это не масштабируется и провоцирует «запишу токен в чат».

---

## 2. Область аудита и ограничения

**Прочитано целиком:** весь `src/`, тесты, LaunchDaemon, installer, menubar, `scripts/nusb`, CI, README, PROBLEMS.md, история из 14 коммитов (`ba8fd65` → `06fe265`).

**iScan.** Репозиторий `https://github.com/chumafox/iScan` из текущего README **не существует либо полностью приватный и не расшарен**. Восстановлено по контракту:

- исходный README ссылался на локальный соседний каталог `../iScan`;
- CLI: `iscan info`, `iscan report [--open]`;
- ставится через `uv tool install` (фаза 5 трекера);
- пишет HTML и строку `✓ Report saved: <path>` (парсит меню-бар);
- ходит в устройство через `pymobiledevice3` и `USBMUXD_SOCKET_ADDRESS`;
- на железе подтверждены `iscan info` / `iscan report` через `/tmp/usbmuxd.sock`.

Выводы по iScan — про **контракт интеграции и то, каким iScan должен быть, чтобы пара работала в магазинах**. Это не ревью его внутреннего кода.

---

## 3. Как устроена пара

```
┌─ магазин ─────────────────────────────────────┐     tailnet / LAN      ┌─ мастер ─────────────────────────────┐
│  iPhone ──USB── Apple usbmuxd                 │                        │  NetworkUSBMenu.app (супервизор)     │
│                 /var/run/usbmuxd              │                        │    └─ usbmuxd-bridge (TLS client)    │
│                      │                        │     TCP+TLS :8721      │           │                           │
│                 usbmuxd-agent (TLS server)    │◄──── AUTH + mux ──────►│    /tmp/usbmuxd.sock  mode 0777      │
│                 LaunchDaemon, root            │      CONNECT/DATA/     │           │                           │
│                 token: /etc/networkusb/token  │      CLOSE/HEARTBEAT   │    USBMUXD_SOCKET_ADDRESS             │
│                 cert:  /etc/networkusb/certs  │                        │           ├─ pymobiledevice3 usbmux   │
│                 tailscaled --tun=userspace    │                        │           └─ iscan report             │
└───────────────────────────────────────────────┘                        └───────────────────────────────────────┘
```

Протокол (9 байт заголовка, big-endian):

| Байт | Поле | Смысл |
|------|------|--------|
| 1 | `msg_type` | CONNECT=1, DATA=2, CLOSE=3, HEARTBEAT=4 |
| 4 | `session_id` | uint32, на bridge монотонный `itertools.count(1)` |
| 4 | `payload_len` | до 4 MiB |
| N | `payload` | сырые байты usbmuxd (plist Listen/Connect/…) |

Это **байтовый туннель**, не прокси уровня lockdown. Pair record, Trust, выбор устройства — всё на стороне iScan/pymobiledevice3 на мастере. Это важное следствие, не документированное как операционный шаг.

---

## 4. Что сделано хорошо

Нельзя чинить то, что уже работает — иначе аудит бесполезен.

1. **Правильная декомпозиция.** Агент не знает про iScan, bridge не знает про USB. Любой клиент usbmuxd (iScan, `pymobiledevice3`, ideviceinfo) работает через один сокет.
2. **Один TLS-сокет на N сессий.** Для диагностики (десятки коротких lockdown-сервисов) это лучше, чем N отдельных TCP.
3. **Явный lifecycle агента.** Отказ от `serve_forever()` + `stop()` / `asyncio.Event` — правильный вывод из дедлока CPython 3.14 (`PROBLEMS.md` §3). Редкий случай, когда баг рантайма закрыт аккуратно.
4. **Flow control через `drain()`, не семафор.** Семафор «N фреймов туда = N фреймов обратно» ломал бы односторонние выгрузки (AFC, crash reports). F-01 закрыт верно.
5. **`--token-file` на агенте.** LaunchDaemon-шаблон больше не кладёт секрет в `ps`. Токен на клиенте генерируется на месте (`openssl rand -hex 32`), в `.pkg` его нет.
6. **Инсталлер с PREFLIGHT.** Идея «двойной клик → магазин в tailnet» правильная для не-IT персонала.
7. **Userspace Tailscale.** Обход GUI «добавить VPN» — осознанный UX-компромисс. Incoming на `:8721` в userspace-режиме tailscaled обычно проксирует на `localhost:8721` (агент слушает `0.0.0.0`, этого достаточно). Исходящие с машины клиента в tailnet не нужны.
8. **Меню-бар вынес `usbmux list` с main thread** (коммит `580d7af`) + таймаут 6 с — после зависания дропдауна это единственно верное решение.
9. **Тесты протокола и TLS добротные.** Round-trip, 5 сессий, TOFU, reconnect после `agent.stop()` — закрывают реальные баги, не только happy-path.
10. **Честный PROBLEMS.md.** Список F-02…F-12 совпал с кодом. Этот аудит его не отменяет, а приоритизирует и дополняет продуктовым слоем.

---

## 5. Находки

Критичность: **P0** — ломает прод / дыра в доступе к устройству; **P1** — будет больно на 3+ магазинах или на реальном отчёте через WAN; **P2** — стоит закрыть в ближайшем цикле; **P3** — гигиена.

### 5.1 Безопасность

#### P0 — локальный UNIX-сокет `0777` в `/tmp` (F-03)

```246:254:src/networkusb/bridge/client.py
        self._unix_server = await asyncio.start_unix_server(
            self._handle_local_client,
            path=self.socket_path,
        )
        try:
            os.chmod(self.socket_path, 0o777)
        except OSError:
            pass
```

Дефолт — `/tmp/usbmuxd.sock`. Любой пользователь и любое ПО на мастере:

- получает полный канал до iPhone магазина (lockdown, AFC, backup, diagnostics);
- может подменить сокет (TOCTOU: `exists` → `unlink` без проверки, что это именно наш SOCK).

`/tmp` на macOS общий. Это не «как у Apple usbmuxd 0666»: тот сокет ведёт на *локально* подключённый телефон, который и так в руках владельца машины. Здесь — на чужой телефон в другом здании.

**Фикс:** `$XDG_RUNTIME_DIR/networkusb/usbmuxd.sock` или `~/Library/Application Support/networkusb/` mode `0700`, сокет `0600`. Перед `unlink` — `stat.S_ISSOCK`. Не продолжать, если stale file не удалился.

#### P0 — токен в argv, в `ps`, в plaintext-конфиге

Агент уже умеет `--token-file`. Bridge и меню-бар — нет.

```217:224:menubar/NetworkUSBMenu/NetworkUSBMenu.swift
        p.arguments = [
            "--agent-host", cfg.agent_host,
            "--agent-port", String(cfg.agent_port),
            "--token", cfg.token,
            "--log-level", "INFO",
        ]
```

`~/.config/usbmuxd-bridge/config.json` содержит токен открытым текстом, права не выставляются. `ps aux` на мастере показывает секрет. Сравнение на агенте — обычный `!=`, в лог пишется `auth_str[:20]` (префикс секрета при опечатке).

**Фикс:** `--token-file` у bridge; меню-бар читает файл сам и передаёт путь, не значение; `chmod 0600` на config; `secrets.compare_digest`; не логировать payload AUTH.

#### P0 — TOFU не защищает первое подключение (F-02)

README обещает «защиту от MITM». Реальность: неизвестный fingerprint **сразу сохраняется**, и только потом уходит `AUTH <token>`.

Активный MITM при первом коннекте (или после удаления `known_hosts`) предъявляет свой сертификат, получает токен, становится «легальным» агентом. Дальше pinning защищает уже *его*.

**Фикс для продакшена:** обязательный `--expected-fingerprint` (агент и так печатает SHA-256 при старте; инсталлер может положить его в `INSTALL_SUMMARY.txt`). TOFU — только с `--tofu` и интерактивным подтверждением. Сравнивать **до** отправки токена (сейчас порядок верный, не хватает отказа при отсутствии pin).

#### P1 — агент слушает `0.0.0.0:8721`

Даже с Tailscale порт торчит на LAN/Wi-Fi магазина. Защита — только токен (который брутфорсится без rate-limit и без constant-time).

**Фикс:** дефолт `127.0.0.1` + `tailscale serve --bg tcp:8721`; либо bind на `tailscale0` IP. ACL tailnet: только машина мастера → `:8721` на теге `tag:shop`.

#### P1 — нет лимитов (F-05)

Нет потолка на TLS-handshake, число bridge, число сессий, размер AUTH-строки сверх дефолтного 64 KiB `StreamReader`, idle timeout, rate неуспешного AUTH. Один получивший токен клиент открывает неограниченно UNIX-соединений к usbmuxd на чужой машине.

Стартовый профиль: handshake 10 с, max 2 bridge, 32 сессии/bridge, 4 MiB очередь/сессию, 5 неверных AUTH / 60 с → drop + бан IP на 5 мин.

#### P2 — неатомарная запись cert / known_hosts (F-11)

`key.pem` пишется, потом `chmod 0600` (окно с umask). Два параллельных старта могут получить рассинхрон cert/key. `known_hosts` — read-modify-write без lock.

**Фикс:** `os.open(..., O_CREAT|O_EXCL, 0o600)` → write → fsync → `os.replace`. File lock на known_hosts.

#### P2 — завышенное обещание безопасности в README

Таблица «TLS + token + pinning = защита от MITM» без оговорок про первое соединение и локальный сокет. Для внутреннего репо это путает приоритеты.

#### P3 — идентификаторы реального устройства в README

UDID и серийник тестового iPhone закоммичены. Репо сейчас private — всё равно лишняя поверхность, если когда-нибудь откроете.

---

### 5.2 Корректность и надёжность туннеля

#### P0 — Head-of-line blocking всех сессий (F-06)

Bridge:

```338:351:src/networkusb/bridge/client.py
        async with self._write_lock:
            self._agent_writer.write(frame)
            await self._agent_writer.drain()
```

Агент в **центральном** dispatch-цикле делает `await sess.writer.drain()` на DATA в usbmuxd. Heartbeat тоже идёт через тот же lock / тот же цикл.

Сценарий, который убьёт `iscan report` на слабом канале:

1. iScan открывает сессию A (AFC, большой crash-log / sysdiagnose).
2. Меню-бар каждые 5 с открывает сессию B (`pymobiledevice3 usbmux list`).
3. Consumer A не успевает читать → `drain()` держит lock → B, CLOSE и HEARTBEAT стоят.
4. Меню-бар по таймауту 6 с рисует «no device», хотя отчёт ещё идёт.
5. Watchdog агента не видит heartbeat 90 с → рвёт весь bridge посреди отчёта.

**Фикс (единственный правильный):** central reader только парсит фрейм и кладёт payload в *bounded* очередь сессии. У каждой сессии свой writer-task. Overflow → CLOSE только этой сессии. Control-plane (HEARTBEAT/CLOSE) — отдельная очередь с приоритетом. Это же закроет регрессию «нет теста на one-way 100 MiB».

#### P1 — UNIX-сокет уничтожается на каждом реконнекте

`_connect_and_serve` в `finally` зовёт `_teardown()`: `unlink(/tmp/usbmuxd.sock)`. Пока backoff (1…30 с) iScan получает `Connection refused`. Идёт отчёт — он умер. Меню-бар мигает серым.

**Фикс:** UNIX-сервер поднимать *один раз* на весь `run()`, не трогать при обрыве TLS. Локальным клиентам, пока агента нет, сразу закрывать соединение (или отвечать usbmux-ошибкой). После reconnect — новые CONNECT. Сокет как stable API.

#### P1 — heartbeat проверяется только на агенте (F-08)

Bridge шлёт HEARTBEAT, **не** проверяет echo, **не** имеет idle-timeout на `_agent_reader_loop`. TCP keepalive вешается на *listening* сокеты агента, не на accepted и не на outbound bridge.

Полуоткрытый туннель: bridge считает себя живым, сокет на месте, iScan висит на `read()` до таймаута ОС.

**Фикс:** echo-deadline на bridge (3 пропущенных ответа); `apply_tcp_keepalive` на *подключённом* сокете с обеих сторон; `ssl_handshake_timeout` + connect timeout 10 с.

#### P1 — backoff без reset / jitter / классификации ошибок (F-09)

Генератор создаётся один раз. После серии обрывов задержка навсегда 30 с, даже если агент уже стабилен. Нет jitter — несколько мастеров (или рестарт всех магазинов) бьют в одну секунду. Fingerprint mismatch и неверный токен крутятся вечно как transient.

**Фикс:** сбрасывать backoff после N секунд успешной работы; `delay * (0.5 + random())`; fatal-ошибки (mismatch, AUTH FAIL) — стоп с понятным UI в меню-баре, не цикл.

#### P1 — валидация протокола (F-10)

- Повторный CONNECT с тем же `session_id` перезаписывает сессию, старый relay в `finally` сделает `sessions.pop` уже *новой* записи.
- `itertools.count` перешагнёт `0xFFFFFFFF` → `struct.pack` упадёт, туннель умрёт.
- DATA в неизвестную сессию порождает CLOSE (можно зафлудить).
- `build_frame` не проверяет диапазон и max payload.
- HEARTBEAT с ненулевым `session_id` / payload принимается.

Мелочи по отдельности, вместе — хрупкий провод, по которому едет диагностика чужих телефонов.

#### P1 — `wait_closed()` без таймаута во внутреннем finally агента

Внешний `_handle_bridge` ограничивает `wait_closed` 1 с. Внутренний `_handle_bridge_inner` — нет. При полуоткрытом TLS внутренний `finally` блокирует задачу, пока её не отменят снаружи.

#### P2 — usbmuxd на старте проверяется через `exists`, не через connect (F-12)

`check_unix_socket_accessible` импортирован и не используется. Регулярный файл / чужие права → агент стартует, каждый CONNECT падает. Для LaunchDaemon нужен wait-loop: сокет появился → работаем, исчез → сессии закрыть, не падать (иначе ThrottleInterval 5 с = crash loop). На macOS Apple usbmuxd обычно живёт всегда, но кастомный путь и Linux этого не гарантируют.

#### P2 — нет обработки SIGTERM

LaunchDaemon шлёт SIGTERM. Python не превращает его в `KeyboardInterrupt`. `stop()` / `_cleanup()` не вызываются — рвутся TLS и usbmuxd-сессии. Нужен `loop.add_signal_handler(SIGTERM, ...)`.

#### P2 — двойной teardown

`_connect_and_serve.finally` и `run()` после цикла оба зовут `_teardown()`. Сейчас почти идемпотентно, но при появлении lock/queue легко получить гонку. Один владелец жизненного цикла.

---

### 5.3 Меню-бар, CLI, контракт с iScan

#### P0 — меню-бар не передаёт `--socket-path`

`Config.socket_path` используется для проверки «сокет есть?» и для `USBMUXD_SOCKET_ADDRESS`, но `startBridge()` **не** передаёт путь в `usbmuxd-bridge`. Bridge всегда поднимает дефолт `/tmp/usbmuxd.sock`.

Расхождение конфига и реальности: статус зелёный / красный не про тот сокет, iScan смотрит не туда. Сейчас маскируется тем, что все хардкодят `/tmp`. Любая попытка закрыть F-03 (приватный путь) **молха сломает меню-бар**, если не чинить вместе.

#### P1 — два разных диалекта `USBMUXD_SOCKET_ADDRESS`

| Кто | Что выставляет |
|-----|----------------|
| README / `bridge/main.py` (до этого аудита) | `unix:/tmp/usbmuxd.sock` (стиль **libusbmuxd**) |
| меню-бар, `scripts/nusb` | `/tmp/usbmuxd.sock` (голый UNIX-путь) |

`pymobiledevice3` (то, чем пользуется iScan) в актуальном `usbmux.py`:

```python
if ":" in usbmux_address:
    hostname, port = usbmux_address.split(":")
    return (hostname, int(port)), socket.AF_INET
return usbmux_address, socket.AF_UNIX
```

Префикс `unix:` → попытка открыть TCP на хост `unix` и порт `/tmp/usbmuxd.sock` → `ValueError`. На железе у вас завелось, потому что меню-бар/ручной запуск, скорее всего, ставили путь **без** `unix:`. README учит обратному.

**Фикс:** везде голый путь. В README явно: «для iScan/pymobiledevice3 — без `unix:`; `unix:` только для libimobiledevice». iScan со своей стороны должен принимать оба и нормализовать.

#### P1 — меню-бар поллит устройство через полный `usbmux list` каждые 5 с

Это отдельная туннельная сессия, конкурирующая с отчётом (см. HOL). Парсинг JSON схемы pymobiledevice3 — скрытый контракт.

**Фикс:** пока `reportRunning` — не поллить; либо один долгоживущий usbmux LISTEN; лучше — лёгкий `STATUS` в протоколе NetworkUSB (число сессий, last heartbeat, список UDID с агента).

#### P1 — «Get Info» парсит человеческую строку iScan

```231:237:menubar/NetworkUSBMenu/NetworkUSBMenu.swift
    /// iscan prints "✓ Report saved: <path>" — return the path from the last line.
    nonisolated static func extractReportPath(from output: String) -> String? {
        for line in output.components(separatedBy: .newlines).reversed() {
            if let range = line.range(of: "Report saved: ") {
                return String(line[range.upperBound...]).trimmingCharacters(in: .whitespaces)
```

Смена формулировки, локаль, `✓`/без галочки, лог в stderr — и HTML не откроется, ошибка проглатывается. Нет таймаута на сам report (list ограничен 6 с, report — нет). Нет прогресса.

**Контракт, который надо зафиксировать в обоих репо:**

```text
iscan report --json-progress
# stdout, по строке:
{"event":"start","udid":"..."}
{"event":"service","name":"lockdown","ok":true}
{"event":"saved","path":"/abs/report.html"}
{"event":"error","code":"not_paired","message":"..."}
```

Меню-бар читает JSON lines, не прозу.

#### P1 — pairing / Trust происходит на мастере, экран — в магазине

Туннель прозрачный → pair record пишет **pymobiledevice3 на Air**, не агент на Pro. Диалог «Доверять этому компьютеру?» вспыхивает на iPhone у продавца, пока мастер жмёт `iscan report` в другом конце города.

Это не баг кода, это дыра в процедуре. Сейчас никак не описано.

Нужно: в iScan — явный шаг `iscan pair --wait` с крупной инструкцией; в меню-баре — пункт «Запросить доверие»; в доке — «продавец нажимает Доверять один раз на магазин+мастер». Копировать pair records с агента бессмысленно: host_id другой.

#### P2 — нет примера `config.json`

Меню-бар при отсутствии файла молча рисует «Tunnel: no config». Инсталлер говорит «пропиши config.json», схемы нет. Поля, восстановленные из Swift:

```json
{
  "agent_host": "100.x.y.z",
  "agent_port": 8721,
  "token": "…",
  "bridge_bin": "/opt/homebrew/bin/usbmuxd-bridge",
  "socket_path": "/tmp/usbmuxd.sock",
  "device_cmd": ["/path/to/pymobiledevice3", "usbmux", "list"],
  "log_path": "/Users/…/Library/Logs/networkusb-bridge.log",
  "report_cmd": ["/path/to/iscan", "report"],
  "report_dir": "/Users/…/Projects/iScan",
  "open_report": true,
  "auto_start": true
}
```

Абсолютные пути к двум разным venv (NetworkUSB и iScan) — хрупкая сцепка. Любой `uv tool upgrade` ломает меню. Схема выложена в `docs/config.example.json`.

#### P2 — нет сборки меню-бара

Один `NetworkUSBMenu.swift`, нет `Package.swift` / xcodeproj / скрипта. `nusb` ждёт `~/Applications/NetworkUSBMenu.app`, который репозиторий не производит. Для второго мастера это reverse-engineering.

#### P2 — один агент на меню-бар

Сеть магазинов = N хвостов. Сейчас: править JSON и рестартить. Нужен список точек (имя, host, fingerprint, token-file) и переключатель в меню.

#### P3 — `nusb` хардкодит `/tmp/usbmuxd.sock`

Даже если config задаёт другой путь, start/status/stop смотрят в `/tmp`.

---

### 5.4 Инсталлер, LaunchDaemon, CI

#### P0 — CI «Build client installer» не может собрать пакет

`installer/third_party/` в `.gitignore`. Workflow на `macos-15` делает checkout и сразу `build_client_pkg.sh`, который **выходит с ошибкой**, если нет `tailscaled` arm64+amd64. `uv` умеет скачать, tailscaled — нет. Кнопка Actions создаёт иллюзию поставки.

Либо git-lfs / release asset с бинарями, либо шаг сборки tailscaled из исходников, либо убрать workflow, пока это не работает.

#### P1 — Python 3.14 в postinstall

Вы уже поймали дедлок `serve_forever` именно на 3.14 и обошли его в коде. Инсталлер всё равно ставит 3.14 на машины клиентов. Pin `3.12` (или 3.13) — меньше сюрпризов CPython, шире колёса.

`pip install -e .` в `/opt/networkusb` для продакшена хуже обычного `pip install .`: editable привязан к дереву исходников, которое потом чистится (`rm -rf installer`). Сейчас исходники остаются — ок, но это dev-режим в проде.

#### P1 — PREFLIGHT требует живой iPhone

Нельзя подготовить Мак магазина утром до прихода устройства. `ioreg … | grep -qi iphone` не видит iPad и завязан на имя в IOKit. usbmuxd на macOS живёт и без телефона — проверка сокета достаточна, телефон — warning, не FAIL.

#### P1 — Tailscale без тегов и ACL

Одноразовый auth key на 1 час — хорошо. Но нода вступает в tailnet с дефолтными правами: магазин видит другие магазины и, возможно, мастер. Скомпрометированный клиентский Мак — пивот.

Минимум: ключ с `tag:networkusb-shop`, ephemeral; ACL «только `tag:networkusb-master` → tcp:8721 на `tag:networkusb-shop`». Задокументировать в `CLIENT_INSTALLER.md`.

Userspace-режим: incoming обычно работает, но это не контракт Tailscale (официальная дока акцентирует SOCKS для исходящих). Имеет смысл после `tailscale up` сделать `tailscale serve --bg tcp:8721` и слушать агентом `127.0.0.1`. Тогда вы не зависите от недокументированного port-forward userspace.

#### P2 — LaunchDaemon

Шаблон уже лучше, чем в первом коммите (нет токена в argv, нет фейкового `Sockets`). Осталось:

- нет `StandardOutPath` / `StandardErrorPath` (агент сам пишет лог — ок, но падения до `basicConfig` теряются);
- нет `UserName` (root не обязателен: `/var/run/usbmuxd` на macOS world-accessible, порт 8721 >1024; root нужен только из-за `/var/log` и `/etc/networkusb` — это лечится владельцем каталогов);
- путь бинаря в шаблоне — плейсхолдер, в postinstall — другая копия plist (расхождение почти гарантировано);
- нет `KeepAlive` по сокету usbmuxd / нет health.

#### P2 — пакет не подписан и не нотаризован

Задокументировано. Для «двойной клик у продавца» без Developer ID это будет «unidentified developer» на каждой второй машине. Либо подпись, либо честный путь «ставим по SSH / `installer -pkg`».

#### P3 — `postinstall` без `set -e`

`set -uo pipefail` + флаг `FAILED`. Часть ошибок глотается (`|| true` на bootstrap). Для идемпотентной переустановки это сознательно, но «Installation Succeeded» при мёртвом агенте уже ловится финальной проверкой `state=running` — хорошо.

---

### 5.5 Качество кода, тесты, поставка

| Тема | Состояние |
|------|-----------|
| Ruff | В PROBLEMS.md — 63 ошибки (BLE001, S110, F401…). В `pyproject` ruff подключён, в CI — нет |
| mypy | `strict = false`, `ignore_missing_imports = true`. На `src/` авторы пишут Success |
| Тесты | 15 protocol + 16 TLS + 4 tunnel. Нет: auth fail, fingerprint mismatch, concurrent sessions, one-way 100 MiB, heartbeat timeout, token-file, CONNECT overwrite, menubar |
| Интеграционные тесты | `asyncio.sleep(0.15/0.3/2.5)` вместо ожидания сокета — флапают под нагрузкой |
| CI на PR | отсутствует (фаза 5.21) |
| `--version` | баннер есть, флага нет |
| `uv tool install` | нет, хотя iScan так ставится — разный UX двух половин одного продукта |
| LICENSE | нет |
| `packages = ["src/networkusb"]` в hatch | проверить `import networkusb` после не-editable install; стандартный src-layout hatchling обычно подхватывает сам |
| `research/` | отладочные скрипты (в т.ч. с багом `tempfile()` в `test_sf2.py`) в основном дереве |
| `exponential_backoff` в `utils.py` | мёртвый код; bridge держит свой генератор |

Сложность `_handle_bridge_inner` (~130 строк, все типы фреймов + auth + watchdog) — главный кандидат на распил: `AuthHandshake`, `SessionMap`, `FrameDispatcher`, `HeartbeatWatchdog`.

---

## 6. Аудит связки с iScan

### 6.1 Что iScan, судя по контракту, делает через туннель

Подтверждено на железе: `pymobiledevice3 usbmux list`, `lockdown info`, `iscan info`, `iscan report`. iOS 17+ `remote start-tunnel` помечен «не требуется» — отчёт, значит, сидит на классических lockdown-сервисах (diagnostics_relay, mobilegestalt, installation_proxy, AFC, crash report mover…), без DVT/RemoteXPC.

Типичный `iscan report` = пачка *параллельных* UNIX-коннектов в usbmuxd. NetworkUSB превращает каждый в сессию. Отсюда критичность HOL и стабильности сокета.

### 6.2 Дыры контракта (чинить в ОБОИХ репо)

| # | Проблема | NetworkUSB | iScan |
|---|----------|------------|-------|
| C1 | Формат `USBMUXD_SOCKET_ADDRESS` | Документировать голый путь; выставлять его в banner | Принимать `unix:/path`, `/path`, `host:port`; нормализовать; дублировать в `PYMOBILEDEVICE3_USBMUX` |
| C2 | Автообнаружение туннеля | Писать `~/.cache/networkusb/active.json` (pid, socket, agent, fingerprint) | Если env пуст — прочитать этот файл, иначе `/var/run/usbmuxd` |
| C3 | Машиночитаемый отчёт | Меню-бар: JSON lines | `iscan report --json-progress`, код выхода ≠ 0 при ошибке |
| C4 | Выбор устройства | Прозрачно отдаёт все UDID агента | `--udid`, интерактивный picker, не «первый попавшийся» |
| C5 | Pairing | Не подменять host_id | `iscan pair --wait` с таймером и текстом для продавца |
| C6 | Обрыв туннеля посреди отчёта | Не unlink-ать сокет; реконнект прозрачно | Таймаут на сервис, retry, partial report вместо полного fail |
| C7 | Пути к бинарям | `uv tool install networkusb` → стабильный `~/.local/bin` | То же для `iscan`. Меню-бар ищет в PATH, не в `.venv` |
| C8 | Версии | `--version` / user-agent в AUTH | В HTML-отчёт писать `iscan X + networkusb Y + agent fingerprint` — иначе не разобрать «это туннель или локальный USB» |
| C9 | iOS 17+ tunnel | Не автоматизировать слепо | Если сервису нужен DVT — запускать `remote start-tunnel` на **мастере** (utun должен быть здесь) и проверять, что usbmux под ним — наш сокет |
| C10 | Несколько магазинов | Multi-agent в меню | `iscan report --agent shop-07` читает каталог NetworkUSB |

### 6.3 Рекомендации внутрь iScan (без исходников — продуктовые)

1. **Считать NetworkUSB first-class транспортом**, не «пользователь сам экспортирует env». Подкоманда `iscan doctor` проверяет: сокет существует, это SOCK, list не пуст, pair record есть, lockdown отвечает за < N мс (по RTT можно понять, что устройство не локальное).
2. **Не держать один жирный синхронный report.** Сетка сервисов с индивидуальным timeout + fail-soft: нет батареи ≠ нет отчёта.
3. **Кэш неизменяемых полей** (ProductType, серийник, UDID) — меньше круглых трипов через WAN.
4. **Прогресс.** Для меню-бара и для продавца («снимаю sysdiagnose, 40%»).
5. **Не тянуть sysdiagnose по умолчанию** через Tailscale userspace. Большие блобы — отдельный флаг `--heavy`. Иначе HOL + 20 минут на отчёт.
6. **Идемпотентный выход.** Код 0 только если HTML записан; 2 — нет устройства; 3 — не paired; 4 — туннель мёртв. Меню-бар сможет показать причину.
7. **Совместный пакет `iscan[remote]`** или метапакет, который ставит оба CLI одной командой — снимет вечную боль двух venv.

### 6.4 Операционная модель, которой сейчас нет

```
1. Мастер генерирует пакет с tagged Tailscale key + ожидаемым hostname.
2. Продавец ставит .pkg (без обязательного iPhone).
3. Агент поднимается, пишет fingerprint+IP в tailnet metadata / INSTALL_SUMMARY.
4. Мастер в меню-баре видит новую точку (whois по тегу), нажимает «Принять»
   → сохраняет fingerprint (не TOFU вслепую) и кладёт token-file.
5. Первый report: iScan показывает «попросите продавца нажать Доверять».
6. Дальше — Get Info из меню, HTML открывается.
```

Сейчас шаги 3–5 — это SSH и ручной JSON. На трёх магазинах уже будет больно.

---

## 7. Производительность

Где будут реальные тормоза (не микрооптимизации Python):

| Место | Почему | Что делать |
|-------|--------|------------|
| HOL + один `drain()` | см. F-06 | per-session queue, приоритет control |
| Tailscale userspace на клиенте | userspace-форвардинг + лишний TLS (WireGuard уже шифрует) | оставить TLS (defense in depth), но измерить; не включать compression пока нет цифр |
| Меню-бар `usbmux list` × 12/мин | лишние сессии | LISTEN / STATUS / пауза на время report |
| CHUNK 64 KiB | на WAN много syscall/frame | 256 KiB DATA; не поднимать `MAX_PAYLOAD` без очередей |
| RSA-2048 + TLS 1.2 | тяжёлый handshake | ECDSA P-256, `minimum_version = TLSv1_3` |
| Нет session resumption | каждый реконнект — полный handshake | TLS tickets |
| Плист usbmux XML | хорошо жмётся, сейчас сырьём | не сжимать, пока нет профиля; если делать — отдельный тип фрейма, чтобы не ломать старых агентов |
| Python asyncio на датаплейне | для lockdown-команд хватит с запасом | не переписывать на Go/Rust, пока нет замера; узкое место — HOL и сеть, не CPU |

Не делать: uvloop (на macOS бесполезен), zero-copy, свой usbmux-парсер на агенте (прозрачность — главное достоинство).

---

## 8. Модель угроз (коротко)

| Угроза | Сейчас | Цель |
|--------|--------|------|
| Сосед по Mac мастера читает iPhone магазина | Да (`0777` + `/tmp`) | Сокет 0600 в приватном каталоге |
| MITM первого коннекта крадёт токен | Да (TOFU) | Обязательный fingerprint |
| Продавец/малварь в LAN магазина стучится на :8721 | Да (`0.0.0.0`, brute-force токена) | bind localhost + serve; rate-limit; ACL |
| Скомпрометированный Мак магазина ходит по tailnet | Да (нет тегов) | `tag:networkusb-shop` только :8721 ← master |
| Утечка токена через `ps`/чат/бэкап конфига | Да | token-file 0600, не argv |
| DoS агента рукопожатиями | Да | лимиты + handshake timeout |
| Подмена stale-сокета в `/tmp` | Да | private dir + проверка S_ISSOCK |

mTLS (клиентский сертификат bridge) — правильный следующий уровень после fingerprint+token, не вместо них.

---

## 9. Сверка с PROBLEMS.md

| ID | Тема | Сейчас |
|----|------|--------|
| F-01 | семафор ломал one-way | **закрыт** (`drain()`) |
| F-07 | deadlock `serve_forever` | **закрыт** (`Event` + `stop()`) |
| F-02 | TOFU / MITM | открыт |
| F-03 | `0777` + `/tmp` | открыт |
| F-04 | токен | **частично** (`--token-file` на агенте; compare/log/bridge/menubar — нет) |
| F-05 | лимиты | открыт |
| F-06 | HOL | открыт, **главный технический долг** |
| F-08 | heartbeat | открыт |
| F-09 | backoff | открыт |
| F-10 | валидация протокола | открыт |
| F-11 | атомарность файлов | открыт |
| F-12 | проверка usbmuxd | открыт |
| LaunchDaemon | plist | **частично** |
| Ruff 63 | стиль | открыт |
| — | `unix:` vs голый путь | **новый**, ломает iScan если следовать README |
| — | menubar не передаёт `--socket-path` | **новый** P0 |
| — | сокет снимается на реконнекте | **новый** P1 |
| — | CI pkg без third_party | **новый** P0 |
| — | нет multi-shop / pairing UX | **новый**, продуктовый |

---

## 10. Дорожная карта

Оценка — один разработчик, знакомый с репо. Не «переписать на Go».

### Фаза A — не ломаться и не светить телефон (3–5 дней)

P0, можно мержить по одному PR:

1. Приватный runtime-dir + сокет `0600`; `unlink` только SOCK.
2. Меню-бар передаёт `--socket-path`; `nusb` читает путь из config.
3. `--token-file` у bridge; меню-бар больше не кладёт токен в argv; `compare_digest`; убрать `auth_str[:20]`.
4. `--expected-fingerprint` (обязателен без `--tofu`).
5. README: `USBMUXD_SOCKET_ADDRESS=/tmp/…` без `unix:`; убрать UDID/серийник.
6. `docs/config.example.json`.
7. Регрессия: one-way ≥64 MiB без ответа доходит целиком.

После фазы A можно чинить путь сокета, не разъезжаясь с iScan.

### Фаза B — отчёт через WAN не зависает (1–1.5 недели)

1. Per-session writer + bounded queue + приоритет HEARTBEAT/CLOSE (F-06).
2. UNIX-сервер живёт весь `run()`, TLS реконнектится под ним.
3. Heartbeat echo + keepalive на connected socket + fatal vs transient reconnect.
4. Backoff reset + jitter.
5. Лимиты сессий/bridge/handshake (F-05).
6. Валидация фреймов, wrap session_id, отказ повторному CONNECT.
7. Меню-бар не поллит `usbmux list` во время report.
8. CI: `ruff` + `pytest` на PR (macos + ubuntu), без pkg.

Это момент, после которого `iscan report` через Tailscale становится скучным (в хорошем смысле).

### Фаза C — сеть магазинов, не два Мака на столе (1–2 недели)

1. Каталог агентов в меню-баре (имя / host / fingerprint / token-file).
2. `iscan report --json-progress` + парсер в Swift; `iscan pair --wait`; `iscan doctor`.
3. `uv tool install` обоих пакетов; меню-бар ищет бинари в PATH.
4. Скрипт сборки `.app` (`swiftc` + bundle + `nusb`).
5. Инсталлер: Python 3.12, PREFLIGHT iPhone = warning, `tailscale serve`, tagged auth key, обычный `pip install .`.
6. Починить или удалить workflow pkg; вендоринг tailscaled задокументировать.
7. SIGTERM → `stop()`; агент ждёт появления usbmuxd, не падает.
8. ACL Tailscale в доке.

### Фаза D — продукт (по потребности)

- mTLS вместо токена.
- Протокол v2 с полем версии (backward compatible: новый `msg_type`).
- STATUS-фрейм (сессии, UDID, rtt) — меню-бар без pymobiledevice3.
- Агент не-root, отдельный user.
- Developer ID + notarization.
- Linux-агент (другой путь usbmuxd).
- Сжатие / TLS 1.3 / ECDSA — по профилю.
- Не тащить `research/` в main.

---

## 11. Что *не* стоит делать сейчас

- Переписывать датаплейн на Go/Rust. Узкое место — дисциплина сессий и UX магазинов, не скорость Python.
- Парсить usbmux plist на агенте («умный прокси»). Потеряете прозрачность, выиграете мало.
- Автоматически копировать pair records с агента на мастер. host_id не совпадёт, Trust всё равно нужен.
- Делать агент HTTP/WebSocket «чтобы проще». Бинарный mux поверх TLS — правильный слой; HTTP добавит HOL и фрейминг.
- Требовать iPhone в PREFLIGHT. Это мешает раскатке.
- Держать два источника правды plist (шаблон + heredoc в postinstall). Один шаблон, `sed` плейсхолдеров.

---

## 12. Предлагаемый порядок ближайших PR

```
PR1  security: private socket 0600 + token-file on bridge + compare_digest
PR2  cli: --expected-fingerprint, --socket-path through menubar/nusb
PR3  docs: USBMUXD_SOCKET_ADDRESS dialect, config.example.json, drop device ids
PR4  relay: per-session queues (F-06) + keep unix server across reconnect
PR5  reliability: heartbeat echo, backoff reset, limits, SIGTERM
PR6  ci: pytest+ruff on PR; quarantine or fix pkg workflow
PR7  iscan contract: --json-progress, doctor, pair --wait   (в репо iScan)
PR8  shops: multi-agent menu + tailscale tags/serve
```

PR1–PR3 безопасны по объёму и сразу закрывают самые дорогие дыры. PR4 — единственный большой дифф в ядре; его нельзя смешивать с косметикой.

---

## 13. Как пользоваться этим документом

- `PROBLEMS.md` оставить как лабораторный журнал конкретных багов.
- Этот файл — продуктово-технический аудит и бэклог.
- Когда iScan станет доступен как репозиторий — пройтись по §6 уже по коду: CLI parser, таймауты сервисов, где выставляется usbmux address, как формируется HTML. Скорее всего всплывут ещё C-пункты (глушение ошибок pymobiledevice3, отсутствие `--udid`, синхронный report в одном event loop).

Если нужно, следующий шаг с этой ветки — **PR1** (сокет + токен) без изменения протокола.
