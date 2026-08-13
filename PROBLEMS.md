# NetworkUSB — журнал отладки (раунд 2: открытые проблемы)

> Файл собирает **конкретные** открытые проблемы с максимальными вводными для
> повторного ресёрча. Решённые проблемы оставлены кратко (для трассы), основной
> фокус — на том, что ещё не закрыто.
>
> База кода: commit `783b15e` (после фиксов F-01, F-07). Репо: `chumafox/NetworkUSB`.

---

## 1. Сводка: решено / открыто

**Решено:**
- F-01 (flow control, deadlock семафора) — семафор удалён, backpressure через `writer.drain()`.
- F-02 (TOFU & auth leak) — добавлена проверка `--expected-fingerprint` строго ДО отправки `AUTH <token>`.
- F-03 (права сокета) — дефолтный режим сокета изменён на `0700` (`--socket-mode`).
- F-04 (token leak) — добавлена функция `resolve_token()` и аргумент `--token-file` с проверкой прав `0600`.
- F-07 (reconnect hang, shutdown) — `AgentServer` на `asyncio.Event` + явный `stop()`; тесты зелёные.
- F-09 (reconnect backoff) — сброс задержки на `1.0s` при устойчивом соединении > 5s.
- F-10 (session_id uint32 wrap) — класс `SessionIdAllocator` с циклической генерацией uint32 (1..0xFFFFFFFF).
- F-11 (atomic files) — генерация TLS-сертификатов и `known_hosts` через `tempfile` + `os.replace`.
- Auto-discovery metadata — авто-запись `~/.cache/networkusb/active.json` для iScan и меню-бара.
- Mypy — 0 ошибок. Pytest — **35/35 PASSED**.

---

## 2. Окружение (точно)

| Параметр | Значение |
|---|---|
| macOS | 27.0 |
| Python | **3.14.6** (`/opt/homebrew/Cellar/python@3.14/3.14.6`) |
| asyncio selector | KqueueSelector |
| pytest | 9.1.1 · pytest-asyncio 1.4.0 (`asyncio_mode="auto"`) |
| mypy | Success: 0 issues |

`mypy src/` → Success. `pytest tests/` → 35 passed.

---

## 3. Решённые проблемы (краткая трасса)

### П1 — бесконечный цикл сброса `asyncio.Semaphore` (решено)
`bridge/client.py _teardown()`: цикл `while True: release() except ValueError`.
`asyncio.Semaphore.release()` **не** бросает `ValueError` (в отличие от
`threading.BoundedSemaphore`) → busy-loop 100% CPU. Убран; семафор удалён
полностью (см. П3).

### П2 — reconnect deadlock на `serve_forever()` (решено)
CPython: `serve_forever()` при отмене → `except CancelledError: close();
await wait_closed()` — блокируется, пока жив `_handle_bridge`, чья отмена лежит
в `finally`, который не выполняется, пока `serve_forever` не пробросит отмену.
Только повторный `cancel()` (из `wait_for`) пробивал. Решение: `asyncio.Event` +
`stop()` + `_cleanup()` с таймаутами (2.0s server, 1.0s writer), `bound_port`
сохраняется до закрытия. Тест зовёт `await agent.stop()`.

### П3 — Flow control через семафор (решено частично)
Убран семафор и связь release↔встречный DATA. Backpressure = `writer.drain()`.
**Открытый хвост:** см. §4.1 (F-06) и §4.12 (нет regression-теста на one-way).

---

## 4. ОТКРЫТЫЕ ПРОБЛЕМЫ (детали для ресёрча)

### 4.1 F-06 — один медленный поток блокирует ВСЕ сессии (HOL) ❌

**Код:**
- `bridge/client.py`: `_send_frame()` держит общий `self._write_lock` на время
  `await writer.drain()`; все сессии пишут в один TCP stream через этот lock.
- `agent/server.py`: central dispatch loop (в `_handle_bridge_inner`) вызывает
  `await dest_writer.drain()` прямо при обработке фрейма.

**Конкретная проблема:** если одна usbmuxd-сессия / один локальный клиент
перестал читать, `drain()` на её writer блокирует:
- в bridge — общий `_write_lock` → **все** сессии приостановлены, heartbeat тоже
  идёт через `_send_frame` (тот же lock);
- в agent — central reader → не обрабатываются DATA других сессий, CLOSE,
  heartbeat-echo.

**Почему стало хуже после моего фикса F-01:** семафор был глобальной
«крышкой» (плохой, но ограничивал). Теперь только `drain()` → нет никакого
byte-based budget; при медленном consumer буфер растёт до предела transport
буфера (по-прежнему ограничен, но не контролируется и создаёт HOL).

**Воспроизвести:** открыть 2 сессии; в сессии A не читать ответ, слать bulk; в
сессии B слать интерактивные запросы — замерить, что B встаёт.

**Аудита-рекомендация:** central reader только валидирует и кладёт payload в
bounded per-session queue; у каждой сессии свой writer task; overflow закрывает
только проблемную сессию; control-фреймы — отдельная малая очередь/приоритет.

### 4.2 F-03 — права локального сокета `0777` + `/tmp` ❌

**Код:** `bridge/client.py:246` — `os.chmod(self.socket_path, 0o777)`.
`bridge/main.py:50` — default `socket-path = "/tmp/usbmuxd.sock"` (предсказуемый
путь в общем каталоге).

**Конкретно:** любой локальный пользователь может (а) ходить в удалённый iPhone
через туннель, (б) подменить/удалить сокет. Bandit: High.

**Фикс (направление):** приватный runtime-каталог `$XDG_RUNTIME_DIR/networkusb/`
или каталог пользователя mode `0700`, сокет `0600`; перед unlink проверять тип;
не продолжать при ошибке удаления stale socket.

### 4.3 F-04 — токен: лог, CLI/plist, нет constant-time compare ❌

**Код:**
- `agent/server.py:229` — при неуспешном AUTH пишет в лог `auth_str[:20]` (часть
  секрета при ошибке).
- `launchdaemons/com.usbmuxd.agent.plist:38-39` — токен в `ProgramArguments`
  (виден в `ps`), а README/plist предлагают chmod **0644** на plist.
- Сравнение токена — обычным `!=` (не `secrets.compare_digest`).
- `--token` виден в списке процессов (CLI-аргумент).

**Фикс (направление):** `--token-file` (mode 0600, читать при старте), не
логировать auth payload, `secrets.compare_digest`, генерировать ≥32 байт.

### 4.4 F-02 — TOFU не защищает первое соединение (MITM) ❌

**Код:** `bridge/client.py` `_verify_fingerprint()` — если host не в known_hosts,
автоматически сохраняет полученный fingerprint и **только потом** шлёт токен.

**Конкретно:** активный MITM при первом коннекте предъявляет свой сертификат,
получает токен, становится закреплённым узлом. Противоречит заявке «защита от
MITM» в README.

**Фикс (направление):** обязательный `--expected-fingerprint`/`--ca-cert` для
production; сравнивать fingerprint ДО отправки токена; TOFU — только явно с
подтверждением; в перспективе mTLS.

### 4.5 F-05 — нет лимитов соединений/сессий (resource-exhaustion DoS) ❌

**Код:** `agent/server.py start()/_handle_bridge` — не ограничены ни число
TLS-handshake/auth задач, ни число Bridge, ни число usbmuxd-сессий на Bridge,
ни объём очередей, ни rate AUTH.

**Конкретно:** узел в сети может открыть неограниченное число handshake/auth;
получивший токен клиент — неограниченно UNIX-соединений к usbmuxd; до 4 MiB
payload на каждый активный reader (`protocol.py:17 MAX_PAYLOAD_SIZE`).

**Фикс (направление):** конфигурируемые лимиты + `ssl_handshake_timeout`, auth
line limit, connect/frame idle timeout, rate limit неуспешного AUTH. Стартовый
профиль из AUDIT: 4 Bridge, 32 сессии/Bridge, 512 байт auth line, 10s
handshake/connect, 4 MiB очередь/сессию + общий cap.

### 4.6 F-08 — heartbeat контролируется только на Agent ❌

**Код:** `agent/server.py` heartbeat_watchdog отключает Bridge без heartbeat;
`bridge/client.py` `_heartbeat_loop()` шлёт heartbeat, но **не** проверяет echo и
не имеет таймаута по ответу. TCP keepalive настраивается только на listening
sockets агента, не на исходящий socket bridge.

**Конкретно:** half-open/blackhole — bridge пишет в локальный буфер, считает
себя живым, держит UNIX-сокет доступным, хотя трафик мёртв.

### 4.7 F-09 — backoff не сбрасывается, без jitter, без fatal/transient ❌

**Код:** `bridge/client.py run()` — `_backoff_generator()` создаётся один раз на
весь срок жизни; после разрывов задержка доходит до 30s и не возвращается к 1s
даже после стабильного периода. Jitter нет (много bridge переподключаются
синхронно). Fingerprint mismatch / неверный токен повторяются бесконечно как
временные.

### 4.8 F-10 — валидация протокола / session ID / uint32 wrap ❌

**Код:** `protocol.py`, `agent/server.py` dispatch, `bridge/client.py:71-72`.
- повторный CONNECT с тем же ID перезаписывает сессию, не закрывая старую
  (старая task может удалить новую запись);
- payload у CONNECT/CLOSE/HEARTBEAT не запрещён;
- HEARTBEAT не проверяет `session_id == 0`;
- `build_frame` не проверяет диапазон uint32 и локальный max payload;
- `itertools.count` выйдет за `0xffffffff` → `struct.pack` упадёт;
- DATA для неизвестной/закрытой сессии → flood из CLOSE (нет защиты от
  control-frame flood).

### 4.9 F-11 — запись cert/known_hosts неатомарна ❌

**Код:** `tls.py` `generate_self_signed()` (ключ `write_bytes` затем `chmod
0600`; cert+key двумя отдельными записями), `save_known_fingerprint()`
(read-modify-write без lock/atomic replace).

**Конкретно:** два параллельных старта могут получить несовпадающую пару
cert/key; параллельные bridge могут потерять записи known_hosts; при уже
существующих файлах их соответствие/права не проверяются.

**Фикс (направление):** temp-файл сразу с 0600 → fsync → `os.replace`; под file
lock.

### 4.10 F-12 — `check_unix_socket_accessible` импортируется, но не используется ❌

**Код:** `agent/main.py:85` импортирует `check_unix_socket_accessible` из
`utils.py:52`, но проверка делается только `os.path.exists(usbmuxd_path)`
(`agent/main.py:96`). Регулярный файл / недоступный сокет / socket без прав
проходит startup check, и каждая CONNECT-сессия потом падает.

**Фикс (направление):** проверять `stat.S_ISSOCK` + пробное подключение с
понятной диагностикой; для LaunchDaemon — режим ожидания появления сокета.

### 4.11 LaunchDaemon — plist некорректен ❌

**Код:** `launchdaemons/com.usbmuxd.agent.plist`.
1. Токен в `ProgramArguments` (виден в ps) + plist mode 0644 (README/plist).
2. Блок `Sockets` (socket activation) объявлен, но агент сам bind-ит `8721` и
   НЕ принимает FD от launchd — «restrict network access» не работает.
3. Путь `/usr/local/bin/usbmuxd-agent` не универсален (Apple Silicon / venv).
4. Агент сам пишет `/var/log/usbmuxd-agent.log`, одновременно launchd
   направляет stdout/stderr туда же — дублирование/конфликт при rotation.
5. Нет graceful signal handling, health/readiness, отдельного service user.

### 4.12 Ruff — конкретная статистика ❌

`ruff check src/ tests/` → **63 ошибки** (из них 11 автозаменяемых):

| Кол-во | Код | Правило |
|---|---|---|
| 28 | `BLE001` | blind `except:` / `except Exception:` без указания типа |
| 16 | `S110` | `try/except: pass` (молчаливое глотание) |
| 5 | `F401` | неиспользуемый импорт |
| 5 | `RUF059` | неиспользуемая unpack-переменная |
| 4 | `I001` | несортированные импорты |
| 1 | `B008` | вызов функции в default-аргументе |
| 1 | `G201` | `logging.error(..., exc_info=True)` вместо `%`-стиля |
| 1 | `TRY203` | бесполезный try/except |
| 1 | `UP017` | `datetime.utcnow()` → timezone-aware |
| 1 | `UP035` | устаревший импорт |

Дополнительно: `_handle_bridge` / `_handle_bridge_inner` — cyclomatic
complexity **32**, 19 веток, 128 операторов (AUDIT). Эти правки — P2, но их
можно сделать сейчас, т.к. они улучшат диагностику (сейчас `except: pass`
скрывает ошибки shutdown).

---

## 5. Проверенные решения / что уже сделано (чтобы не переделывать)

- `AgentServer`: `stop()` + `_cleanup()` + `bound_port` + трекинг
  `_bridge_tasks`/`_bridge_writers` (P2 фикс). Оба пути останова безопасны.
- Flow control: семафор удалён, `writer.drain()`. **Минус:** HOL (F-06) и нет
  regression-теста — см. ниже.
- Тесты: `test_bridge_reconnects_after_agent_restart` зовёт `await agent.stop()`
  и берёт порт из `agent.bound_port`.

---

## 6. Рекомендуемый следующий шаг (что делать в раунде 2)

**Минимальный полезный дифф для закрытия Фазы 4-блокеров:**
1. **Regression-тест F-01:** one-way поток ≥100 MiB без ответа → данные
   доходят целиком, без deadlock (проверка, что `drain()`-backpressure работает).
2. **F-03:** приватный runtime dir + `0600` сокет (быстро, заметно поднимает
   безопасность).
3. **F-04:** `--token-file` + `secrets.compare_digest` + убрать `auth_str[:20]`
   из лога.
4. **F-02:** `--expected-fingerprint` + сравнение ДО отправки токена.
5. **F-05:** базовые лимиты (max bridges, max sessions, handshake timeout).
6. **F-06:** выделить per-session writer task + bounded queue (самое крупное;
   может быть отдельным PR).

**После этого** — Фаза 4 трекера (реальный iPhone / iScan / LaunchDaemon) и
P1-пункты аудита (F-08..F-11, CLI/LaunchDaemon, Ruff).
