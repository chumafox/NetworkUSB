# NetworkUSB — журнал проблем при отладке

Файл фиксирует проблемы, с которыми я (CLI-агент) столкнулся в ходе отладки
интеграционных тестов (Фаза 3 трекера). Обновляется по мере работы.

Статус меток:
- ✅ решено (в коде)
- 🔄 в процессе / остаётся открытым

---

## Проблема 1 — бесконечный цикл сброса семафора в bridge ✅

**Симптом.** `tests/test_tunnel.py` виснет на этапе teardown: `gather()` не
возвращается, event loop «крутится» на 100% CPU (в `sample` — непрерывный
`task_step`).

**Стек (через `sys._current_frames()`):**
```
bridge/client.py:445 in _teardown
bridge/client.py:189 in _connect_and_serve
bridge/client.py:103 in run
```

**Корень.** В `_teardown()` был такой «сброс» flow-control семафора:
```python
while True:
    try:
        self._flow_sem.release()
    except ValueError:
        break  # semaphore already at max — done
```
Автор подразумевал поведение `threading.BoundedSemaphore`, где `release()`
сверх границы бросает `ValueError`. Но это **`asyncio.Semaphore`**, у которого
`release()` границы не проверяет и `ValueError` **не бросает** — просто
инкрементирует `_value` бесконечно. Цикл никогда не выходит → бесконечный
busy-loop → луп блокируется.

**Фикс.** Убрал сломанный цикл из `_teardown()` и вместо него пересоздаю
семафор заново на каждое подключение в `_connect_and_serve()`:
```python
self._flow_sem = asyncio.Semaphore(FLOW_CONTROL_LIMIT)
```
Эффект «сброса в полный» тот же, но без зависания.

---

## Проблема 2 — graceful shutdown агента виснет, если bridge ещё подключён ✅

**Симптом.** Тест `test_bridge_reconnects_after_agent_restart` виснет на
`await asyncio.gather(agent_task, ...)` после `agent_task.cancel()`. Event loop
при этом **idle** (в `select`) — это не busy-loop, а вечное ожидание.

**Стек:**
```
agent/server.py  start()  →  async with self._server  →  server.wait_closed()
```

**Корень.** `AgentServer.start()` делал:
```python
async with self._server:
    await self._server.serve_forever()
```
При отмене (`cancel()`) контекстный менеджер зовёт `server.close()` +
`server.wait_closed()`. `wait_closed()` ждёт завершения всех активных
обработчиков подключений. Bridge ещё подключён и висит в `read_frame()`
(ждёт следующий фрейм), поэтому `_handle_bridge` не завершается → `wait_closed()`
висит вечно.

Почему тест 1 (`test_basic_roundtrip`) проходил: там в teardown сначала
отменяли **bridge** — тот закрывал соединение с агентом, агент получал EOF,
`_handle_bridge` завершался, и только потом `wait_closed()` возвращался.
В тесте reconnect отменяют **агента** при живом bridge → дедлок.

Это не только тестовая проблема: реальный агент при остановке (SIGTERM /
`launchctl stop`) должен принудительно закрывать активные bridge-подключения.

**Фикс.** Агент теперь трекает активные хендлеры bridge:
- в `__init__`: `self._bridge_tasks: set[asyncio.Task] = set()`;
- `_handle_bridge` регистрирует `asyncio.current_task()` и снимает его в `finally`;
- `start()` при выходе сначала **отменяет** все `_bridge_tasks`, дожидается их
  (`gather`), и только потом `server.close()` + `wait_closed()`.

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

---

## Статус на текущий момент

Первые 3 интеграционных теста после Проблемы 1 проходят:
- `test_basic_roundtrip` ✅
- `test_multiple_sequential_sessions` ✅
- `test_fingerprint_saved_on_first_connect` ✅

После фикса Проблемы 2 (агент закрывает bridge при shutdown) ожидается, что
4-й тест тоже пройдёт. Если `test_tunnel.py` по-прежнему виснет — обновить этот
файл, указав новый стек/симптом.
