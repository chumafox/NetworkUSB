# NetworkUSB — журнал отладки (максимум вводных данных)

> Файл собирает **все** факты, наблюдения, дампы, эксперименты и гипотезы,
> накопленные при отладке интеграционных тестов (Фаза 3 трекера), для
> глубокого ресёрча. Ничего не отфильтровано: включаю и то, что не до конца
> объяснено, и противоречия.
>
> База кода: commit `b4bb576` (после моих фиксов). Репо: `chumafox/NetworkUSB`.

---

## 1. Окружение (точно, важно для воспроизводимости)

| Параметр | Значение |
|---|---|
| macOS | 27.0 |
| Python | **3.14.6** (`/opt/homebrew/Cellar/python@3.14/3.14.6`) |
| asyncio selector | KqueueSelector |
| pytest | 9.1.1 |
| pytest-asyncio | 1.4.0 (`asyncio_mode = "auto"`) |
| pytest-cov | 7.1.0 |
| Целевой Python по pyproject | `>=3.11` |

Проект ставится как editable в `.venv`. Тесты: `tests/test_protocol.py` (15),
`tests/test_tls.py` (16) — **проходят**. `tests/test_tunnel.py` (4) — **1–3
проходят, 4-й (reconnect) висит**.

---

## 2. Проблема 1 — бесконечный цикл сброса семафора в bridge ✅ (РЕШЕНА)

**Файл:** `src/networkusb/bridge/client.py`, `_teardown()`.

**Симптом.** Тесты висли на teardown; event loop «крутился» на 100% CPU
(в `sample` — непрерывный `task_step`, main-thread в `task_wakeup_lock_held`).

**Стек (через `sys._current_frames()` из фонового потока):**
```
bridge/client.py:445 in _teardown
bridge/client.py:189 in _connect_and_serve
bridge/client.py:103 in run
```

**Корень.** В `_teardown()` был цикл:
```python
while True:
    try:
        self._flow_sem.release()
    except ValueError:
        break  # "semaphore already at max — done"
```
Автор исходил из поведения `threading.BoundedSemaphore` (release сверх границы
бросает `ValueError`). Но `self._flow_sem` — это **`asyncio.Semaphore`**, у
которого `release()` **не имеет границы и не бросает `ValueError`** — просто
инкрементирует `_value`. Цикл бесконечный → busy-loop → луп залипает.

**Проверка факта (независимо):** в asyncio `Semaphore.release()` =
`self._value += 1; self._wake_up_next()` — исключение не кидает (в отличие от
`threading.BoundedSemaphore`).

**Мой фикс (в `b4bb576`):**
- убрал сломанный цикл из `_teardown()`;
- в `_connect_and_serve()` пересоздаю семафор на каждое подключение:
  ```python
  self._flow_sem = asyncio.Semaphore(FLOW_CONTROL_LIMIT)
  ```
Эффект «сброса в полный» тот же, но без зависания.

**Важно (незакрытый хвост):** это лечит только *зависание teardown*. Сам
**дизайн** flow control неверен — см. Проблему 3 (F-01). Пересоздание семафора
не устраняет дефект «release привязан к встречному DATA».

---

## 3. Проблема 2 — reconnect-тест виснет ✅ (РЕШЕНА)

**Тест:** `tests/test_tunnel.py::test_bridge_reconnects_after_agent_restart`.
Стабильно виснет (AUDIT: падал 5/5). Это и был «красный» тест Фазы 3.
**Сейчас зелёный: все 4 integration-теста и весь набор (35/35) проходят.**

### 3.0 Корневая причина + решение (подтверждено ресёрчем)

**Корень (объяснение подтверждено — см. переданный анализ и воспроизведено):**
`asyncio.Server.serve_forever()` в CPython при отмене делает примерно так:
```python
try:
    await self._serving_forever_fut
except CancelledError:
    self.close()
    await self.wait_closed()   # ← блокирует, пока живой _handle_bridge
    raise
```
Дедлок:
1. `at.cancel()` → `serve_forever()` ловит `CancelledError` и вызывает
   `await self.wait_closed()`.
2. `wait_closed()` ждёт завершения всех активных `_handle_bridge`.
3. `_handle_bridge` ждёт отмены, которая была в `finally` метода `start()`.
4. `finally` не выполняется, пока `serve_forever()` не пробросит отмену выше.
→ взаимное ожидание. Только **повторный** `cancel()` (из `wait_for`) пробивает
внутренний `await wait_closed()` и запускает `finally` — это и объясняет
наблюдения (2-й cancel работает).

**Решение (реализовано):** не гнать shutdown через отмену `serve_forever()`.
`AgentServer` теперь управляет циклом через `asyncio.Event`:
- `start()`: биндит listener, сохраняет `bound_port`, ждёт `_stop_event.wait()`;
  `except CancelledError: pass` + `finally: await self._cleanup()`;
- `stop()`: `_stop_event.set()` + `await self._cleanup()`;
- `_cleanup()`: закрывает `_bridge_writers` → отменяет и собирает
  `_bridge_tasks` → `server.close()` + `wait_closed()` **с таймаутом 2.0s**;
- `_handle_bridge` трекает и task, и writer; `writer.wait_closed()` — с
  таймаутом 1.0s.
- добавлен `bound_port` — тест берёт порт из него ДО закрытия сервера
  (был баг: `agent._server.sockets[0]` читался ПОСЛЕ закрытия).
- тест вызывает `await agent.stop()` вместо `agent_task.cancel()`.

Обе ветки останова безопасны: и `await agent.stop()`, и `task.cancel()`.


### 3.1 Симптом

- В `pytest` тест проходит setup (bridge-сокет создан) и дальше не завершается.
- В изолированном сценарии зависает на строке:
  ```python
  at.cancel()
  mock.close()
  await asyncio.gather(at, return_exceptions=True)   # ← виснет здесь
  ```
  где `at = asyncio.create_task(agent.start())`.

### 3.2 Репродукция (скрипты, которые можно запустить)

| Скрипт | Что делает | Результат |
|---|---|---|
| `/tmp/nusb_rec.py` | полный reconnect-сценарий (mock usbmuxd + agent + bridge + roundtrip + cancel agent + restart) | виснет на `gather(at)`; стек гл. потока: idle в `select` (loop жив, но `await gather` не возвращается) |
| `/tmp/nusb_rec2.py` | то же + дамп **тасков** через `run_coroutine_threadsafe` (таймер 10с) | см. дамп ниже |
| `/tmp/nusb_rec3.py` | то же + DEBUG-логирование в `/tmp/rec3_debug.log` + `wait_for(gather,6)` | см. хронологию |
| `/tmp/test_sf.py` | чистый `serve_forever()` без соединений + `cancel()` | **останавливается корректно** (CancelledError) |
| `/tmp/test_sf2.py` | `serve_forever()` + активное (pending TLS-handshake) подключение + `cancel()` | **НЕ останавливается; TIMEOUT** |
| `/tmp/test_sf3.py` | `srv.close() + wait_closed()` + pending TLS-подключение | **TIMEOUT**, sf.done()=False |

Запуск из корня проекта: `.venv/bin/python -u /tmp/nusb_rec2.py` и т.п.
Скрипты лежат в `/tmp`, но их стоит перенести в репо при доработке.

### 3.3 Хронология отладочного запуска (`rec3`, `/tmp/rec3_debug.log`)

```
12:34:44,432  asyncio: Using selector: KqueueSelector
12:34:44,491  agent: Agent server listening on ('127.0.0.1', 59738)
12:34:44,794  bridge: Connecting to agent
12:34:44,804  bridge: TCP+TLS handshake complete
12:34:44,805  agent: Incoming bridge connection
12:34:44,805  agent: Bridge authenticated successfully
12:34:44,805  bridge: Authenticated to agent
12:34:44,806  bridge: Local UNIX socket ready
12:34:45,295  bridge: Local client → session 1
12:34:45,296  agent: CONNECT session 1
12:34:45,297  agent: Session 1: usbmuxd closed connection
12:34:45,297  bridge: Agent closed session 1
12:34:45,297  bridge: Session 1 local client closed
12:34:45,298  agent: CLOSE session 1
   ↑ roundtrip ОК, local client закрыт, агент получил CLOSE
   ─── здесь вызывается at.cancel() (~45.3) ───
   [ДАЛЕЕ ~6 секунд НИЧЕГО]
12:34:51,502  bridge: Agent connection error: 0 bytes read on a total of 9 expected bytes
12:34:51,502  bridge: Reconnecting to agent in 1 s...
12:34:51,503  agent: Bridge cleaned up; closed 0 usbmuxd session(s)
   ↑ :51.5 — это сработал wait_for(6): он отменил gather → отменил at →
     finally агента выполнился → bridge увидел закрытие → «cleaned up»
12:34:56,509  bridge: Bridge stopped
```

**Главный вывод хронологии:** явный `at.cancel()` в ~45.3 **не** вызвал
очистку. Очистка произошла только через ~6с, когда `wait_for` **повторно**
отменил `at`. То есть первый `cancel()` «не сработал», второй — сработал.

### 3.4 Дамп стека тасков (`rec2`, момент ~10с после старта)

```
--- task bridge-heartbeat  done=False cancelled=False
      client.py:375 in _heartbeat_loop
--- task agent            done=False cancelled=False
      server.py:119 in start            ← это «await serve_forever()»
--- task Task-1           done=False cancelled=False
      /tmp/nusb_rec2.py:78 in main      ← «await asyncio.gather(at)»
--- task bridge           done=False cancelled=False
      client.py:103 in run
--- task Task-4           done=False cancelled=False
      server.py:153 in _handle_bridge   ← «await _handle_bridge_inner(...)»
--- task Task-5           done=False cancelled=False
      server.py:251 in heartbeat_watchdog
```

**Противоречие:** `at.cancel()` вызван (~1.2с), но к 10с таск `agent` —
`cancelled=False` и всё ещё в `serve_forever()`. При этом loop **жив** (bridge
переподключается, heartbeat_watchdog крутится). Почему отмена не доставлена —
не объяснено. (Ср. с `wait_for`, чья отмена — доставилась.)

### 3.5 Три эксперимента по `serve_forever` (точные выводы)

1. **`test_sf.py`** — `serve_forever()` без соединений, `cancel()`:
   → `serve_forever raised CancelledError`, `cancelled()=True`. Чистая отмена
   работает. **Значит сам по себе `serve_forever` отменяем.**

2. **`test_sf2.py`** — то же, но перед `cancel()` открыто raw-соединение на
   TLS-порт (handshake не завершён):
   → `>>> TIMEOUT: serve_forever did NOT stop on cancel`; затем после повторной
   отмены `cancelled()=True done()=True`.
   **Наличие активного/pending соединения ломает отмену; повторная отмена
   добивает.** Это 1-в-1 поведение reconnect-теста.

3. **`test_sf3.py`** — вместо `cancel()`: `srv.close() + await srv.wait_closed()`
   при pending TLS-подключении:
   → `>>> wait_closed() TIMEOUT`, `sf.done()=False`.
   **`wait_closed()` ждёт все активные обработчики; незавершённый TLS-handshake
   держит его вечно.**

### 3.6 Ключевые наблюдения (итог)

- Loop остаётся живым (bridge: heartbeat, reconnect работают) — это **не**
  busy-loop и не блокировка loop; это «вечное ожидание» в `await gather(at)`.
- Явная отмена таска `serve_forever` не доставляется/не останавливает при
  живом соединении; повторная отмена — доставляет.
- `wait_closed()` виснет при любом активном обработчике (в т.ч. pending
  handshake, и, по логике, при живом `_handle_bridge`, читающем фреймы).
- В тесте bridge-сокет после «restart» не пересоздаётся (assert на
  `os.path.exists(bridge_sock)` не достигается из-за зависания раньше).

### 3.7 Что я уже попробовал (фиксы) и почему не сработало

**Фикс A (в `b4bb576`):** `AgentServer.start()` теперь:
```python
try:
    await self._server.serve_forever()
finally:
    for task in list(self._bridge_tasks):
        task.cancel()
    if self._bridge_tasks:
        await asyncio.gather(*self._bridge_tasks, return_exceptions=True)
    self._server.close()
    await self._server.wait_closed()
```
+ `_handle_bridge` регистрирует `asyncio.current_task()` в `self._bridge_tasks`
и снимает в `finally`; добавлен прослойка `_handle_bridge_inner`.

**Результат:** reconnect всё равно виснет. Т.е. «force-close bridge-хендлеров в
finally агента» недостаточно: либо отмена до finally не доходит (см. 3.4),
либо `await writer.wait_closed()` внутри finally хендлера сам виснет (не
проверено точно — см. гипотезы).

### 3.8 Гипотезы (для ресёрча)

1. **Гипотеза H1 (отмена не доставляется).** В Python 3.14 при живом
   connection `cancel()` на таске `serve_forever` не прерывает его (см.
   test_sf2). Причина — во внутренней обработке `Server.serve_forever()`
   + `wait_closed()` в его cleanup, который блокируется на активном
   хендлере. Надо смотреть исходник `asyncio/base_events.py` / `Server`
   в 3.14.

2. **Гипотеза H2 (`writer.wait_closed()` в finally хендлера).** В
   `_handle_bridge_inner` в `finally` есть `writer.close(); await
   writer.wait_closed()`. Для TLS-writer к всё ещё подключённому bridge это
   может не завершиться (close_notify / flush). Тогда `gather(bridge_tasks)`
   в finally агента висит. **Нужно проверить**: заменить на
   `writer.close()` без `await wait_closed()`, либо `await
   asyncio.shield(...)`/timeout.

3. **Гипотеза H3 (тест читает порт после закрытия сервера — из AUDIT F-07).**
   Тест делает:
   ```python
   agent_task.cancel()
   mock_server.close()
   await asyncio.gather(agent_task, return_exceptions=True)
   ...
   agent_port = agent._server.sockets[0].getsockname()[1]  # ← ПОСЛЕ закрытия!
   ```
   После закрытия `self._server.sockets` пуст → `IndexError`, а не зависание.
   Но это отдельный баг теста: **порт надо сохранять до остановки**. Не
   объясняет зависание, но мешает после исправления основной причины.

4. **Гипотеза H4 (архитектурная, из AUDIT F-07).** Правильно не полагаться на
   `task.cancel()`: дать `AgentServer` явные `async close()/wait_closed()`,
   которые (а) `server.close()`, (б) закрывают все активные bridge-хендлеры
   (cancel + timeout на `wait_closed`), (в) затем `wait_closed()` с deadline.
   Тест звать `await agent.close()` вместо `agent_task.cancel()`.

### 3.9 Что говорит AUDIT.md (ваш отчёт) — по reconnect-тесту

Из F-07 (дословно):
> «отмена `agent.start()` закрывает listener, но не активное bridge-соединение
> в том же event loop; Bridge не замечает „рестарт“ и socket остаётся».
> «порт надо сохранить до остановки» (про `agent._server.sockets[0]` после
> закрытия).
> Рекомендация: явные `async start()/close()/wait_closed()`, `TaskGroup` для
> connection/session tasks, cancellation-safe `finally`, ожидание всех writers,
> идемпотентный teardown. `stop()` должен ставить event и закрывать transport.

Также F-07 перечисляет: `_handle_local_client` глотает `CancelledError` и не
пробрасывает; client handler tasks не трекаются и не ожидаются при teardown;
ряд `StreamWriter` закрывается без `await wait_closed()`.

---

## 4. Проблема 3 — Flow control сломан по дизайну (F-01, Critical) ❌ (НЕ РЕШЕНА)

**Файл:** `src/networkusb/bridge/client.py:83-84` (семафор), `297-304`
(acquire/release), `347-349` (release по DATA).

**Суть (из AUDIT F-01 + моё подтверждение):**
Bridge захватывает один слот семафора на каждый исходящий DATA-фрейм, а
освобождает только при получении встречного DATA. В байтовом туннеле встречный
DATA **не является ACK**: соотношение потоков 0:1, 1:N, N:1.

**Следствие:** если локальный клиент отправил 100 chunk-ов (по 64KiB ≈ 6.25
MiB), а устройство ещё не ответило, 101-й chunk ждёт вечно. Семафор общий для
Bridge → одна сессия блокирует исходящую передачу всех сессий. Отдельное
воспроизведение из AUDIT:
```
after_100ms done=False data_frames_sent=100 semaphore_locked=True
```

**Мой фикс (пересоздание семафора при коннекте) НЕ решает это** — он лишь
убрал зависание teardown. Для корректной работы нужно (по AUDIT):
- убрать связь release со встречным DATA;
- минимально — полагаться на `writer.write() + await writer.drain()`
  (transport backpressure);
- предпочтительно — один dedicated writer task + bounded очередь по **байтам**;
- при необходимости — добавить в протокол `WINDOW_UPDATE`/ACK;
- regression-тест: ≥100 MiB одностороннего потока без ответа.

---

## 5. Прочие находки AUDIT, которые стоит учесть при ресёрче

- **Mypy:** 1 ошибка — несовместимое присваивание `UsbmuxdSession | None` в
  переменную, выведенную как `UsbmuxdSession` (`agent/server.py:262`).
- **Ruff:** 59 замечаний (26 broad except, 15 silent pass, импорты); у
  `_handle_bridge` cyclomatic complexity 32, 19 веток, 128 операторов.
- **Bandit:** 1 High (chmod 0777 локального сокета), 3 Medium.
- **Безопасность (F-02..F-05):** TOFU не защищает первое соединение; сокет
  `/tmp/usbmuxd.sock` + chmod 0777; токен в CLI/plist/логах (первые 20 символов
  AUTH строки пишутся в лог при неуспехе); нет лимитов соединений/сессий.
- **Прочее (F-08..F-12):** heartbeat только на агенте; backoff не сбрасывается
  и без jitter; нет валидации фреймов/uint32 wrap; запись cert/known_hosts
  неатомарна; `check_unix_socket_accessible` есть в `utils.py`, но CLI его не
  использует.

Полный разбор — в `AUDIT.md` (ветка `arena/019fef0d-networkusb`, 465 строк).

---

## 6. Статус и приоритет

**Текущее состояние:**
- Проблема 1 (semaphore loop) — решена.
- Проблема 2 (reconnect hang, F-07) — решена. Весь набор 35/35 зелёный.
- Проблема 3 (F-01, flow control) — **решена**: семафор, привязанный к встречному
  DATA, удалён; backpressure через `writer.drain()`. Но **нет** regression-теста
  на длинный односторонний поток (≥100 MiB) — стоит добавить (см. AUDIT F-01).
- Mypy — 0 ошибок. Ruff — остаются предсуществующие замечания (broad except,
  complexity) — это P2.

**Связь с трекером (README):** Фаза 3 (интеграционные тесты 13a–13d) теперь
завершена. Дальше — Фаза 4 (реальный iPhone / iScan / LaunchDaemon) и P0-пункты
аудита (безопасность: F-02..F-05).

**Рекомендуемые следующие шаги:**
1. Добавить regression-тест на one-way поток (F-01) и, желательно, на
   stop()/graceful-close без pending tasks.
2. Закрыть P0 аудита: F-03 (права сокета), F-04 (token file + compare_digest +
   убрать секрет из логов), F-05 (лимиты), F-02 (fingerprint до токена).
3. Обновить CLI, если `AgentServer` API изменился (bound_port/stop).
