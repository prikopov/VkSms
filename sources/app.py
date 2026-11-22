import datetime as dt
import msvcrt
import os
import queue
import sqlite3
import sys
import threading
import tkinter as tk
import webbrowser
from contextlib import closing
from pathlib import Path
from tkinter import messagebox, ttk

import bot

ROOT = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent
MODES = {"Демо": "demo", "Поиск без SMS": "search", "Поиск и отправка SMS": "live"}
LABELS = {
    "queued": "В очереди", "expired": "Пост устарел", "submitted": "Ожидает Android",
    "sent": "Отправлено", "failed": "Ошибка отправки", "review": "Проверить вручную",
    "demo": "Демо",
}


def display_time(timestamp):
    return dt.datetime.fromtimestamp(timestamp).strftime("%d.%m %H:%M:%S")


class App:
    def __init__(self, root, data_dir):
        self.root = root
        self.data_dir = data_dir
        self.config_path = data_dir / "settings.json"
        self.inbox = queue.Queue()
        self.stop = threading.Event()
        self.worker = None
        self.closing = False
        self.urls = {}
        self.table_signature = None
        root.title("VkSms — мониторинг ВКонтакте")
        root.geometry("1040x720")
        root.minsize(860, 620)
        root.protocol("WM_DELETE_WINDOW", self.close)

        style = ttk.Style(root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Title.TLabel", font=("Segoe UI", 21, "bold"))
        style.configure("Treeview", rowheight=25)

        frame = ttk.Frame(root, padding=18)
        frame.pack(expand=True, fill="both")
        ttk.Label(frame, text="VkSms", style="Title.TLabel").pack(anchor="w")
        ttk.Label(frame, text="Свежие посты ВКонтакте и SMS через Android по USB").pack(anchor="w")
        setup = ttk.LabelFrame(frame, text="Настройки", padding=10)
        setup.pack(fill="x", pady=(12, 10))
        setup.columnconfigure(1, weight=1)
        setup.columnconfigure(3, weight=1)

        try:
            config = bot.settings(self.config_path)
        except (OSError, ValueError, TypeError):
            config = bot.DEFAULTS.copy()
            messagebox.showwarning("VkSms", "Не удалось прочитать настройки. Загружены значения по умолчанию.")
        self.fields = {}
        self.inputs = []
        self.add_entry(setup, 0, 0, "Токен ВК", "token", config["token"], show="•")
        self.add_entry(setup, 0, 2, "Серийный номер ADB", "serial", config["serial"])
        self.add_entry(setup, 1, 0, "Фраза поиска", "phrase", config["phrase"])
        self.add_entry(setup, 1, 2, "Свежесть, минут", "freshness", config["freshness"])
        self.add_entry(setup, 2, 0, "Интервал, минут (5–10)", "interval", config["interval"])
        ttk.Label(setup, text="Режим").grid(row=2, column=2, padx=(10, 6), sticky="w")
        self.mode = tk.StringVar(value="Демо")
        self.mode_input = ttk.Combobox(setup, textvariable=self.mode, values=list(MODES), state="readonly")
        self.mode_input.grid(row=2, column=3, sticky="ew", pady=3)
        self.mode_input.bind("<<ComboboxSelected>>", lambda event: self.refresh())
        ttk.Label(setup, text="Текст SMS").grid(row=3, column=0, sticky="nw", pady=4)
        self.sms = tk.Text(setup, height=2, wrap="word", font=("Segoe UI", 10), undo=True)
        self.sms.grid(row=3, column=1, columnspan=3, sticky="ew", pady=3)
        self.sms.insert("1.0", config["sms"])
        self.inputs.append(self.sms)

        bar = ttk.Frame(frame)
        bar.pack(fill="x", pady=(0, 10))
        self.buttons = []
        for title, action in (
            ("Запустить", lambda: self.start(True)),
            ("Один цикл", lambda: self.start(False)),
            ("Сохранить", self.save),
            ("Установить APK", lambda: self.android_task(True)),
            ("Проверить телефон", lambda: self.android_task(False)),
        ):
            button = ttk.Button(bar, text=title, command=action)
            button.pack(side="left", padx=(0, 7))
            self.buttons.append(button)
        self.stop_button = ttk.Button(bar, text="Остановить", command=self.ask_stop, state="disabled")
        self.stop_button.pack(side="left")
        ttk.Button(bar, text="Папка данных", command=lambda: os.startfile(self.data_dir)).pack(side="right")

        history = ttk.LabelFrame(frame, text="Номера и статус", padding=6)
        history.pack(expand=True, fill="both")
        columns = ("phone", "date", "post", "state", "detail")
        self.table = ttk.Treeview(history, columns=columns, show="headings", selectmode="browse")
        for key, title, width, stretch in (
            ("phone", "Номер", 125, False), ("date", "Дата поста", 110, False),
            ("post", "Публикация (двойной щелчок)", 240, True),
            ("state", "Статус", 145, False), ("detail", "Подробности", 270, True),
        ):
            self.table.heading(key, text=title)
            self.table.column(key, width=width, minwidth=75, stretch=stretch)
        scroll = ttk.Scrollbar(history, orient="vertical", command=self.table.yview)
        self.table.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.table.pack(expand=True, fill="both")
        self.table.bind("<Double-1>", self.open_post)

        log_frame = ttk.LabelFrame(frame, text="Журнал", padding=6)
        log_frame.pack(fill="x", pady=(10, 0))
        self.log_list = tk.Listbox(log_frame, height=4, font=("Segoe UI", 9))
        self.log_list.pack(fill="x")
        self.status = tk.StringVar(value="Демо готово. Нажмите «Один цикл» для проверки.")
        ttk.Label(frame, textvariable=self.status, wraplength=800).pack(anchor="w", pady=(7, 0))

        for mode in ("demo", "search"):
            with closing(bot.database(self.db_path(mode))):
                pass
        self.tick()

    def add_entry(self, setup, row, col, label, key, value, **options):
        ttk.Label(setup, text=label).grid(row=row, column=col, padx=(0 if col == 0 else 10, 6), sticky="w")
        self.fields[key] = tk.StringVar(value=value)
        entry = ttk.Entry(setup, textvariable=self.fields[key], **options)
        entry.grid(row=row, column=col + 1, sticky="ew", pady=3)
        self.inputs.append(entry)

    def read_config(self):
        values = {key: field.get().strip() for key, field in self.fields.items()}
        values["sms"] = self.sms.get("1.0", "end-1c").strip()
        try:
            values["interval"] = int(values["interval"])
            values["freshness"] = int(values["freshness"])
        except ValueError:
            raise bot.BotError("Интервал и свежесть должны быть целыми числами.") from None
        if not 5 <= values["interval"] <= 10:
            raise bot.BotError("Интервал поиска должен быть от 5 до 10 минут.")
        if not values["interval"] <= values["freshness"] <= 60:
            raise bot.BotError("Свежесть должна быть не короче интервала и не больше 60 минут.")
        if len(values["sms"].encode("utf-16-le")) // 2 > 400:
            raise bot.BotError("Текст SMS должен быть не длиннее 400 символов UTF-16.")
        return values

    def save(self):
        try:
            bot.save_settings(self.config_path, self.read_config())
            self.status.set("Настройки сохранены.")
        except (OSError, bot.BotError) as error:
            messagebox.showerror("VkSms", str(error), parent=self.root)

    def db_path(self, mode):
        return self.data_dir / ("demo.sqlite3" if mode == "demo" else "recipients.sqlite3")

    def start(self, repeat):
        mode = MODES[self.mode.get()]
        try:
            config = self.read_config()
            if mode != "demo" and (not config["token"] or not config["phrase"]):
                raise bot.BotError("Для поиска заполните токен ВК и фразу.")
            if mode == "live" and not config["sms"]:
                raise bot.BotError("Для отправки введите текст SMS.")
            bot.save_settings(self.config_path, config)
        except (OSError, bot.BotError) as error:
            messagebox.showerror("VkSms", str(error), parent=self.root)
            return
        self.launch(lambda: bot.run(self.db_path(mode), config, ROOT, mode, self.stop, self.inbox.put, repeat),
                    "Проверяю новые посты…" if mode != "demo" else "Запускаю демо…")

    def android_task(self, install):
        preferred = self.fields["serial"].get().strip()

        def task():
            adb = bot.find_adb(ROOT)
            serial = bot.select_device(adb, preferred)
            if install:
                apk = ROOT / "VkSmsUSB.apk"
                if not apk.is_file():
                    raise bot.BotError("Распакуйте VkSmsUSB.apk рядом с VkSms.exe.")
                reply = bot.adb_run(adb, "-s", serial, "install", "-r", str(apk), timeout=90)
                if "Success" not in reply:
                    raise bot.BotError("Телефон отклонил установку APK.")
                bot.adb_run(adb, "-s", serial, "shell", "am", "start", "-n", "ru.vksms.relay/.MainActivity")
                self.inbox.put("APK установлен. На телефоне нажмите «Разрешить отправку SMS».")
            else:
                reply = bot.relay(adb, serial, "ping")
                self.inbox.put("Телефон готов к отправке SMS." if reply["state"] == "ready" else reply["detail"])

        self.launch(task, "Устанавливаю APK…" if install else "Проверяю USB-соединение…", cancellable=False)

    def launch(self, action, message, cancellable=True):
        if self.worker is not None:
            return
        self.stop.clear()
        self.status.set(message)
        self.set_busy(True, cancellable)

        def target():
            try:
                action()
            except (bot.BotError, OSError, sqlite3.Error) as error:
                self.inbox.put(f"Ошибка: {error}")
            except Exception:
                self.inbox.put("Внутренняя ошибка. Программа остановлена.")
                import traceback
                (self.data_dir / "error.txt").write_text(traceback.format_exc(), encoding="utf-8")
            finally:
                self.inbox.put(None)

        self.worker = threading.Thread(target=target, daemon=True)
        self.worker.start()

    def set_busy(self, busy, cancellable=False):
        state = "disabled" if busy else "normal"
        for widget in self.buttons + self.inputs:
            widget.configure(state=state)
        self.mode_input.configure(state="disabled" if busy else "readonly")
        self.stop_button.configure(state="normal" if cancellable else "disabled")

    def ask_stop(self):
        self.stop.set()
        self.status.set("Останавливаю после текущей операции…")
        self.stop_button.configure(state="disabled")

    def open_post(self, event):
        row_id = self.table.identify_row(event.y)
        url = self.urls.get(row_id, "")
        if url.startswith("https://vk.com/wall"):
            webbrowser.open(url)

    def refresh(self):
        path = self.db_path(MODES[self.mode.get()])
        try:
            with closing(sqlite3.connect(path, timeout=0.1)) as db:
                rows = db.execute("""
                    SELECT phone, post_time, post_url, state, detail
                    FROM recipients ORDER BY updated DESC, found_at DESC LIMIT 500
                """).fetchall()
                events = db.execute("SELECT at, message FROM events ORDER BY id DESC LIMIT 50").fetchall()
        except sqlite3.Error:
            return
        signature = (rows, events)
        if self.table_signature == signature:
            return
        self.table_signature = signature
        selection = self.table.selection()
        self.table.delete(*self.table.get_children())
        self.urls.clear()
        for phone, date, url, state, detail in rows:
            self.urls[phone] = url
            self.table.insert("", "end", iid=phone, values=(
                phone, display_time(date), url, LABELS.get(state, state), detail,
            ))
        if selection and self.table.exists(selection[0]):
            self.table.selection_set(selection[0])
        self.log_list.delete(0, "end")
        for timestamp, message in events:
            self.log_list.insert("end", f"{display_time(timestamp)}  {message}")

    def tick(self):
        while True:
            try:
                message = self.inbox.get_nowait()
            except queue.Empty:
                break
            if message is None:
                self.worker = None
                if self.closing:
                    self.root.destroy()
                    return
                if self.stop.is_set():
                    self.status.set("Остановлено.")
                self.set_busy(False)
            elif not self.stop.is_set():
                self.status.set(message)
        self.refresh()
        self.root.after(750, self.tick)

    def close(self):
        if self.worker is None:
            self.root.destroy()
        else:
            self.closing = True
            self.ask_stop()


def main():
    root = tk.Tk()
    root.withdraw()
    bot.APP_DIR.mkdir(parents=True, exist_ok=True)
    with (bot.APP_DIR / "instance.lock").open("a+b") as lock:
        try:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            messagebox.showinfo("VkSms", "Программа уже открыта.", parent=root)
            root.destroy()
            return
        App(root, bot.APP_DIR)
        root.deiconify()
        root.mainloop()


if __name__ == "__main__":
    main()
