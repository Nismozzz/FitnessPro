"""安装版入口：打开窗口、启动本机服务，并在浏览器中使用。"""

from __future__ import annotations

import logging
import multiprocessing
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from logging.handlers import RotatingFileHandler

import tkinter as tk
from tkinter import messagebox

import uvicorn

URL = "http://127.0.0.1:8000"


def configure_logging() -> str:
    from app.config import writable_root

    folder = writable_root()
    folder.mkdir(parents=True, exist_ok=True)
    log_path = folder / "fitnesspro.log"
    handler = RotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[handler],
    )
    return str(log_path)


def healthy() -> bool:
    try:
        with urllib.request.urlopen(URL + "/health", timeout=1) as response:
            return response.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def main() -> None:
    log_path = configure_logging()
    root = tk.Tk()
    root.title("FitnessPro")
    root.geometry("480x190")
    root.resizable(False, False)
    status = tk.Label(root, text="正在启动 FitnessPro…", justify="left", anchor="w", wraplength=440)
    status.pack(fill="both", expand=True, padx=18, pady=16)
    open_button = tk.Button(root, text="在浏览器中打开", state="disabled", command=lambda: webbrowser.open(URL))
    open_button.pack(pady=(0, 16))

    server = uvicorn.Server(uvicorn.Config("app.main:app", host="127.0.0.1", port=8000, log_level="info"))
    thread: threading.Thread | None = None
    started_here = False

    def serve() -> None:
        try:
            server.run()
        except Exception:
            logging.getLogger("fitnesspro").exception("服务退出")

    def boot() -> None:
        nonlocal thread, started_here
        if healthy():
            status.config(text="FitnessPro 已在运行。关闭此窗口不会停止已有服务。")
            open_button.config(state="normal")
            webbrowser.open(URL)
            return
        started_here = True
        thread = threading.Thread(target=serve, name="fitnesspro-server", daemon=True)
        thread.start()
        for _ in range(60):
            if healthy():
                status.config(text=f"已启动。关闭此窗口会停止服务。\n日志：{log_path}")
                open_button.config(state="normal")
                webbrowser.open(URL)
                return
            time.sleep(0.5)
        status.config(text=f"启动超时。请查看日志：\n{log_path}")
        messagebox.showerror("FitnessPro", f"服务没有在 30 秒内就绪。\n{log_path}")

    def on_close() -> None:
        if started_here:
            server.should_exit = True
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    threading.Thread(target=boot, daemon=True).start()
    root.mainloop()
    if thread is not None:
        thread.join(timeout=5)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
