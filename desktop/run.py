# -*- coding: utf-8 -*-
"""Windows 桌面程序入口。

用法：
    python desktop/run.py                 启动服务 + 打开桌面应用窗口
    python desktop/run.py --no-window     只跑后台服务（定时任务照常）
    python desktop/run.py --selfcheck     命令行一键自检后退出
    python desktop/run.py --fetch flash   命令行手动跑一次快报后退出
    python desktop/run.py --host 0.0.0.0  允许局域网访问（默认仅 127.0.0.1）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from server import Instance, serve  # noqa: E402
from engine.monitor import selfcheck  # noqa: E402


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="日常小助手 · Windows 桌面程序")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-window", action="store_true")
    ap.add_argument("--selfcheck", action="store_true")
    ap.add_argument("--fetch", choices=["prices", "news", "flash", "daily", "full"])
    ap.add_argument("--site", choices=["bilibili", "tieba", "weibo", "zhihu", "douyin"],
                    help="打开多平台中心的官方页面窗口（由主程序拉起）")
    ap.add_argument("--send-mail", action="store_true")
    a = ap.parse_args()

    if a.site:
        from engine.multisite import show_official_window
        show_official_window(a.site, os.path.join(ROOT, "desktop", "data"),
                             os.path.join(ROOT, "desktop", "GoldDesk.ico"))
        return

    # 同一端口只允许一个实例；后台无窗口实例不占 GUI 锁，GUI 可复用已有服务
    if not (a.selfcheck or a.fetch):
        from engine.single import ensure_single, port_in_use
        if not ensure_single(a.port, gui=not a.no_window):
            print("检测到本程序已在运行，已切换到现有窗口。", flush=True)
            return
        if port_in_use(a.port) and not a.no_window:
            from launcher import open_desktop_window, install_tray
            install_tray(os.path.join(ROOT, "desktop", "GoldDesk.ico"))
            open_desktop_window("http://127.0.0.1:%d/" % a.port, "日常小助手", 1440, 900,
                                icon_path=os.path.join(ROOT, "desktop", "GoldDesk.ico"),
                                data_dir=os.path.join(ROOT, "desktop", "data"),
                                profile_dir=os.path.join(ROOT, "desktop", "data", "browser-profile"))
            return
        if port_in_use(a.port) and a.no_window:
            print("同一端口已有实例在运行，后台实例不再重复启动。", flush=True)
            return

    inst = Instance(ROOT, a.port, a.host)
    try:
        from engine.housekeeping import run_housekeeping
        cleaned = run_housekeeping(inst)
        if any(cleaned.values()):
            print("本端清理：%s" % cleaned, flush=True)
    except Exception as e:
        print("本端清理跳过：%s: %s" % (type(e).__name__, e), flush=True)
    if a.selfcheck:
        print(json.dumps(selfcheck(inst.cfg, inst.db, inst.pipeline), ensure_ascii=False, indent=2))
        inst.scheduler.stop()
        return
    if a.fetch:
        kind = {"prices": "prices", "news": "news", "flash": "flash",
                "daily": "daily_summary", "full": "full"}[a.fetch]
        r = inst.pipeline.run(kind=kind, trigger="manual", send_mail=a.send_mail, force=True)
        print(json.dumps({k: r.get(k) for k in ("ok", "message", "finished_at", "delivery")},
                         ensure_ascii=False, indent=2, default=str))
        inst.scheduler.stop()
        return

    url = "http://%s:%d/" % ("127.0.0.1" if a.host in ("0.0.0.0", "::") else a.host, a.port)
    print("桌面程序地址：%s" % url)
    print("本端数据目录：%s" % inst.data_dir)
    print("提示：点击界面顶栏「退出」按钮可同时停止窗口与后台服务；Ctrl+C 亦可完全退出。")
    if a.no_window:
        serve(inst)
        return
    # WebView2 窗口必须跑在主线程：后台服务放子线程，窗口关闭后再把服务停掉
    import time
    from server import stop_http
    from launcher import open_desktop_window, install_tray
    threading.Thread(target=serve, args=(inst,), daemon=True).start()
    for _ in range(50):
        if getattr(__import__("server"), "HTTPD", None) is not None:
            break
        time.sleep(0.1)
    install_tray(os.path.join(ROOT, "desktop", "GoldDesk.ico"))
    open_desktop_window(url, "日常小助手", 1440, 900,
                        icon_path=os.path.join(ROOT, "desktop", "GoldDesk.ico"),
                        data_dir=inst.data_dir,
                        profile_dir=os.path.join(inst.data_dir, "browser-profile"))
    print("应用窗口已关闭，后台服务同步停止。", flush=True)
    stop_http()


if __name__ == "__main__":
    main()
