# -*- coding: utf-8 -*-
"""GoldDesk.exe 的 PyInstaller 入口（桌面端）。

与源码模式 desktop/run.py 的区别：
  * UI 资源随 exe 打进临时解压目录，持久化数据固定放在 exe 同目录的 data/；
  * 无控制台，日志写 data/stdout.log；支持 --stop 干净退出；
  * 已有一个实例在跑时不重复启动，只打开应用窗口。
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading
import urllib.request


def app_paths():
    """返回 (根路径, UI 目录, 数据目录)。"""
    if getattr(sys, "frozen", False):
        base = os.path.dirname(os.path.abspath(sys.executable))
        mei = getattr(sys, "_MEIPASS", base)
        ui = os.path.join(mei, "ui")
        if not os.path.isdir(ui):
            ui = os.path.join(base, "ui")
        data = os.path.join(base, "data")
    else:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        ui = os.path.join(base, "desktop", "ui")
        data = os.path.join(base, "desktop", "data")
    os.makedirs(data, exist_ok=True)
    return base, ui, data


def prepare_stdio(data_dir):
    """windowed 打包没有控制台，把 stdout/stderr 落到 data/stdout.log 便于排障。"""
    try:
        if getattr(sys, "frozen", False):
            f = open(os.path.join(data_dir, "stdout.log"), "a",
                     encoding="utf-8", errors="replace", buffering=1)
            sys.stdout = f
            sys.stderr = f
        elif sys.stdout is not None:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def port_busy(host: str, port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.4)
    try:
        return s.connect_ex((host, port)) == 0
    finally:
        s.close()


def post_shutdown(port: int) -> dict:
    req = urllib.request.Request(
        "http://127.0.0.1:%d/api/shutdown" % port,
        data=b"{}", headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    base, ui_dir, data_dir = app_paths()
    prepare_stdio(data_dir)
    sys.path.insert(0, getattr(sys, "_MEIPASS", base))
    icon_path = os.path.join(getattr(sys, "_MEIPASS", base), "GoldDesk.ico")
    if not os.path.isfile(icon_path):
        icon_path = os.path.join(base, "desktop", "GoldDesk.ico")

    from server import Instance, serve           # noqa: E402
    from launcher import open_desktop_window   # noqa: E402
    from engine.monitor import selfcheck         # noqa: E402

    ap = argparse.ArgumentParser(description="黄金积存金助手 · 桌面端独立实例")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-window", action="store_true", help="只跑后台服务，不打开窗口")
    ap.add_argument("--stop", action="store_true", help="让已运行的本端实例退出")
    ap.add_argument("--selfcheck", action="store_true", help="一键自检后退出（有输出窗口时可见）")
    ap.add_argument("--fetch", choices=["prices", "news", "flash", "daily", "full"])
    ap.add_argument("--send-mail", action="store_true")
    a = ap.parse_args()

    if a.stop:
        try:
            print(json.dumps(post_shutdown(a.port), ensure_ascii=False))
        except Exception as e:
            print("没有可退出的实例：%s" % e)
        return

    url = "http://%s:%d/" % ("127.0.0.1" if a.host in ("0.0.0.0", "::") else a.host, a.port)
    profile = os.path.join(data_dir, "browser-profile")
    if port_busy("127.0.0.1", a.port) and not (a.selfcheck or a.fetch):
        print("检测到本端实例已在运行，直接打开窗口：%s" % url)
        if not a.no_window:
            open_desktop_window(url, "黄金积存金助手 v1.0", 1440, 900, icon_path=icon_path,
                                data_dir=data_dir, profile_dir=profile)
        return

    inst = Instance(base, a.port, a.host, dir_name="desktop",
                    data_dir=data_dir, ui_dir=ui_dir)
    if a.selfcheck:
        print(json.dumps(selfcheck(inst.cfg, inst.db, inst.pipeline),
                         ensure_ascii=False, indent=2, default=str))
        inst.scheduler.stop()
        return
    if a.fetch:
        kind = {"prices": "prices", "news": "news", "flash": "flash",
                "daily": "daily_summary", "full": "full"}[a.fetch]
        r = inst.pipeline.run(kind=kind, trigger="manual",
                              send_mail=a.send_mail, force=True)
        print(json.dumps({k: r.get(k) for k in ("ok", "message", "finished_at", "delivery")},
                         ensure_ascii=False, indent=2, default=str))
        inst.scheduler.stop()
        return

    print("桌面程序地址：%s" % url)
    print("本端数据目录：%s" % data_dir)
    print("退出方式：界面顶栏「退出」按钮 / GoldDesk.exe --stop / 任务管理器结束进程")
    if not a.no_window:
        # WebView2 窗口必须跑在主线程：后台服务放子线程，窗口关闭后再把服务停掉
        import time
        from server import stop_http
        threading.Thread(target=serve, args=(inst,), daemon=True).start()
        for _ in range(50):
            if getattr(__import__("server"), "HTTPD", None) is not None:
                break
            time.sleep(0.1)
        open_desktop_window(url, "黄金积存金助手 v1.0", 1440, 900, icon_path=icon_path,
                            data_dir=data_dir, profile_dir=profile)
        print("应用窗口已关闭，后台服务同步停止。", flush=True)
        stop_http()
        time.sleep(0.8)
        return
    serve(inst)


if __name__ == "__main__":
    main()
