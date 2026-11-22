import base64
import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import closing
from pathlib import Path

APP_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "VkSms"
VK_URL = "https://api.vk.com/method/newsfeed.search"
COMPONENT = "ru.vksms.relay/.UsbReceiver"
PHONE = re.compile(
    r"(?<![\w+])(?:\+7|[78])[ \t\u00a0\u202f–-]*"
    r"(?:\([0-9]{3}\)|[0-9]{3})[ \t\u00a0\u202f–-]*"
    r"[0-9]{3}[ \t\u00a0\u202f–-]*[0-9]{2}[ \t\u00a0\u202f–-]*[0-9]{2}(?!\w)"
)
DEFAULTS = {
    "token": "", "phrase": "ищу подработку", "sms": "",
    "interval": 5, "freshness": 20, "serial": "",
}
FINAL_STATES = {"sent", "failed", "review"}


class BotError(Exception):
    pass


def settings(config_path):
    try:
        return DEFAULTS | json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return DEFAULTS.copy()


def save_settings(config_path, data):
    temporary = config_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(config_path)


def extract_phones(body):
    return {"+7" + re.sub(r"[^0-9]", "", match.group())[1:] for match in PHONE.finditer(body)}


def phrase_key(value):
    return " ".join(value.casefold().replace("ё", "е").split())


def request_vk(token, params):
    payload = urllib.parse.urlencode(params | {"access_token": token, "v": "5.199"}).encode()
    request = urllib.request.Request(VK_URL, data=payload, headers={"User-Agent": "VkSms/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            data = json.load(response)
    except urllib.error.HTTPError as exc:
        raise BotError(f"ВК вернул HTTP {exc.code}.") from None
    except (OSError, ValueError):
        raise BotError("Не удалось получить ответ от ВК.") from None
    if "error" in data:
        code = data["error"].get("error_code", "?")
        hint = {
            5: "Проверьте токен.", 6: "Лимит запросов. Повторим в следующем цикле.",
            7: "У токена нет прав на поиск.", 15: "У токена нет доступа к поиску.",
            27: "Используйте токен приложения или пользователя с доступом к поиску.",
        }.get(code, "Проверьте доступ к newsfeed.search.")
        raise BotError(f"Ошибка ВК {code}. {hint}")
    response = data.get("response")
    if not isinstance(response, dict) or not isinstance(response.get("items"), list):
        raise BotError("ВК вернул неожиданный формат результатов.")
    return response


def search(config, now, should_stop):
    oldest = now - config["freshness"] * 60
    params = {"q": config["phrase"], "start_time": oldest, "end_time": now, "count": 100}
    cursors = set()
    phrase_pattern = re.compile(r"(?<!\w)" + re.escape(phrase_key(config["phrase"])) + r"(?!\w)")
    while not should_stop():
        response = request_vk(config["token"], params)
        for item in response["items"]:
            date = item.get("date")
            owner = item.get("owner_id") or item.get("source_id")
            post = item.get("id") or item.get("post_id")
            body = item.get("text")
            if not isinstance(date, int) or not oldest <= date <= now:
                continue
            if not isinstance(owner, int) or not isinstance(post, int) or not isinstance(body, str):
                continue
            if phrase_pattern.search(phrase_key(body)):
                for phone in sorted(extract_phones(body)):
                    yield phone, f"https://vk.com/wall{owner}_{post}", date
        cursor = response.get("next_from")
        if not cursor:
            return
        if cursor in cursors or len(cursors) >= 49:
            raise BotError("ВК прервал выдачу. Сохранены уже полученные результаты.")
        cursors.add(cursor)
        params["start_from"] = cursor


def demo_search(now):
    # The fictional number has an invalid area code. Demo never uses the USB adapter.
    text = "Ищу подработку. +7 (000) 000-00-01 и 8 000 000 00 01"
    for phone in sorted(extract_phones(text)):
        yield phone, "Пример записи №1", now
        yield phone, "Пример записи №2", now


def database(path):
    db = sqlite3.connect(path, timeout=6)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode = WAL")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS recipients (
            phone TEXT PRIMARY KEY,
            post_url TEXT NOT NULL,
            post_time INTEGER NOT NULL,
            found_at INTEGER NOT NULL,
            job TEXT NOT NULL UNIQUE,
            state TEXT NOT NULL DEFAULT 'queued',
            detail TEXT NOT NULL DEFAULT '',
            device TEXT,
            attempted_at INTEGER,
            updated INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS recipients_queue ON recipients(state, post_time);
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY,
            at INTEGER NOT NULL,
            message TEXT NOT NULL
        );
    """)
    return db


def log(db, message):
    with db:
        db.execute("INSERT INTO events(at, message) VALUES (?, ?)", (int(time.time()), message))


def ingest(db, matches):
    inserted = 0
    for phone, url, post_time in matches:
        now = int(time.time())
        with db:
            inserted += db.execute("""
                INSERT INTO recipients(phone, post_url, post_time, found_at, job, updated)
                VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(phone) DO UPDATE SET
                    post_url = excluded.post_url, post_time = excluded.post_time,
                    updated = excluded.updated, state = 'queued', detail = ''
                WHERE recipients.state IN ('queued', 'expired')
                    AND excluded.post_time > recipients.post_time
            """, (phone, url, post_time, now, uuid.uuid4().hex, now)).rowcount
    return inserted


def set_status(db, job, state, detail=""):
    with db:
        db.execute("""
            UPDATE recipients SET state = ?, detail = ?, updated = ? WHERE job = ?
        """, (state, detail, int(time.time()), job))


def adb_run(adb, *args, timeout=20):
    try:
        result = subprocess.run(
            [str(adb), *args], capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        raise BotError("ADB не ответил. Проверьте USB-соединение.") from None
    except OSError:
        raise BotError("Не удалось запустить ADB. Проверьте Android Platform Tools.") from None
    if result.returncode:
        raise BotError("Команда ADB не выполнена. Проверьте устройство и разрешение отладки.")
    return result.stdout


def find_adb(root):
    sdk = Path(os.environ.get("LOCALAPPDATA", "")) / "Android/Sdk/platform-tools/adb.exe"
    for candidate in (root / "platform-tools/adb.exe", sdk):
        if candidate.is_file():
            return candidate
    command = shutil.which("adb")
    if command:
        return command
    raise BotError("ADB не найден. Поместите папку platform-tools рядом с программой.")


def usb_devices(adb):
    output = adb_run(adb, "devices", "-l")
    devices = []
    for line in output.splitlines():
        fields = line.split()
        if len(fields) < 2 or fields[1] != "device":
            continue
        serial = fields[0]
        # TCP, wireless debugging and emulators are not USB phones.
        if ":" not in serial and not serial.startswith(("emulator-", "adb-")):
            devices.append(serial)
    return devices


def select_device(adb, preferred):
    devices = usb_devices(adb)
    if preferred:
        if preferred in devices:
            return preferred
        raise BotError("Указанный телефон не найден среди разрешённых USB-устройств.")
    if len(devices) == 1:
        return devices[0]
    if not devices:
        raise BotError("Подключите Android по USB и разрешите отладку на телефоне.")
    raise BotError("Подключено несколько телефонов. Укажите серийный номер ADB.")


def relay(adb, serial, action, **fields):
    payload = base64.urlsafe_b64encode(
        json.dumps(fields | {"op": action}, ensure_ascii=False).encode()
    ).decode().rstrip("=")
    output = adb_run(adb, "-s", serial, "shell", "am", "broadcast", "--user", "0",
                     "--receiver-foreground", "-a", "ru.vksms.relay.USB",
                     "-n", COMPONENT, "--es", "payload", payload)
    match = re.search(r'Broadcast completed: result=82, data="([A-Za-z0-9_-]+)"', output)
    if not match:
        raise BotError("Android-модуль не ответил. Установите APK и откройте его на телефоне.")
    try:
        value = match.group(1)
        return json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
    except ValueError:
        raise BotError("Android-модуль вернул повреждённый ответ.") from None


def refresh_job(db, job, result):
    state = result.get("state")
    if state in FINAL_STATES:
        set_status(db, job, state, result.get("detail", ""))
    elif state not in ("pending", "missing"):
        set_status(db, job, "review", result.get("detail", "Неизвестный ответ Android."))


def reconcile(db, adb, is_stopped):
    rows = db.execute("""
        SELECT job, device, attempted_at FROM recipients WHERE state = 'submitted'
    """).fetchall()
    if not rows or is_stopped():
        return
    devices = usb_devices(adb)
    for row in rows:
        if is_stopped():
            return
        if row["device"] not in devices:
            continue
        try:
            result = relay(adb, row["device"], "status", id=row["job"])
        except BotError:
            continue
        refresh_job(db, row["job"], result)
        if result.get("state") in ("pending", "missing") and time.time() - row["attempted_at"] >= 600:
            set_status(db, row["job"], "review", "Android не подтвердил результат в течение 10 минут.")


def dispatch(db, config, root, mode, stop):
    if stop.is_set():
        return
    if mode == "search":
        return
    if mode == "live":
        adb = find_adb(root)
        reconcile(db, adb, stop.is_set)
    cutoff = int(time.time()) - config["freshness"] * 60
    with db:
        db.execute("""
            UPDATE recipients SET state = 'expired', detail = 'Пост вышел из окна свежести', updated = ?
            WHERE state = 'queued' AND post_time < ?
        """, (int(time.time()), cutoff))
    if mode == "demo":
        with db:
            db.execute("""
                UPDATE recipients SET state = 'demo', detail = 'Имитация отправки', updated = ?
                WHERE state = 'queued'
            """, (int(time.time()),))
        return

    rows = db.execute("""
        SELECT * FROM recipients WHERE state = 'queued' ORDER BY post_time DESC LIMIT 50
    """).fetchall()
    if not rows or stop.is_set():
        return
    serial = select_device(adb, config["serial"])
    ready = relay(adb, serial, "ping")
    if ready.get("state") != "ready":
        raise BotError(ready.get("detail", "Телефон не готов к отправке."))
    for row in rows:
        if stop.is_set():
            return
        now = int(time.time())
        if row["post_time"] < now - config["freshness"] * 60:
            set_status(db, row["job"], "expired", "Пост вышел из окна свежести")
            continue
        with db:
            changed = db.execute("""
                UPDATE recipients SET state = 'submitted', device = ?, attempted_at = ?, updated = ?
                WHERE job = ? AND state = 'queued'
            """, (serial, now, now, row["job"])).rowcount
        if not changed:
            continue
        result = relay(adb, serial, "send", id=row["job"], phone=row["phone"], text=config["sms"])
        if result.get("state") == "missing":
            set_status(db, row["job"], "review", "Android не подтвердил приём задания.")
        else:
            refresh_job(db, row["job"], result)
        if stop.wait(1):
            return


def run(bot_db, config, root, mode, stop, report, repeat):
    with closing(database(bot_db)) as db:
        while not stop.is_set():
            started = time.monotonic()
            now = int(time.time())
            try:
                matches = demo_search(now) if mode == "demo" else search(config, now, stop.is_set)
                count = ingest(db, matches)
                message = f"Поиск завершён. Новых или обновлённых записей: {count}."
                log(db, message)
                report(message)
            except BotError as exc:
                log(db, str(exc))
                report(str(exc))
            try:
                dispatch(db, config, root, mode, stop)
            except BotError as exc:
                log(db, str(exc))
                report(str(exc))
            if not repeat:
                if mode == "live":
                    for _ in range(4):
                        if not db.execute("SELECT 1 FROM recipients WHERE state = 'submitted' LIMIT 1").fetchone():
                            break
                        if stop.wait(2):
                            break
                        try:
                            reconcile(db, find_adb(root), stop.is_set)
                        except BotError:
                            break
                return
            delay = config["interval"] * 60
            while not stop.is_set():
                remaining = delay - (time.monotonic() - started)
                if remaining <= 0:
                    break
                if stop.wait(min(15, remaining)):
                    return
                if mode == "live":
                    try:
                        reconcile(db, find_adb(root), stop.is_set)
                    except BotError:
                        pass
