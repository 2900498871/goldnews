# -*- coding: utf-8 -*-
"""SMTP 邮件通道（FR-14 / FR-04）：测试连接、测试发信、重试、去重、失败留痕。"""
from __future__ import annotations

import smtplib
import time
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from .util import DISCLAIMER, bj_str, iso_bj, now_bj, redact


class Mailer:
    def __init__(self, cfg_store, db, logger):
        self.cfg_store = cfg_store
        self.db = db
        self.log = logger

    def _settings(self, override: dict | None = None):
        cfg = self.cfg_store.get()
        s = dict(cfg.get("smtp", {}))
        if override:
            s.update(override)
        cred = self.cfg_store.secret("smtp.credential")
        s["_credential"] = cred or ""
        return s

    def _connect(self, s: dict):
        host, port = s.get("host"), int(s.get("port") or 465)
        timeout = int(s.get("timeout_seconds") or 20)
        sec = s.get("security", "ssl_tls")
        if sec == "ssl_tls":
            cli = smtplib.SMTP_SSL(host, port, timeout=timeout)
        else:
            cli = smtplib.SMTP(host, port, timeout=timeout)
            cli.ehlo()
            if sec == "starttls":
                cli.starttls()
                cli.ehlo()
        if s.get("user"):
            if not s.get("_credential"):
                cli.quit()
                raise RuntimeError("未录入邮箱授权码（SMTP 授权码，非登录密码）")
            cli.login(s["user"], s["_credential"])
        return cli

    def test_connection(self, override: dict | None = None) -> dict:
        s = self._settings(override)
        if not s.get("host") or not s.get("user"):
            return {"ok": False, "message": "SMTP 服务器或发件邮箱未填写"}
        try:
            cli = self._connect(s)
            code, msg = cli.noop()
            cli.quit()
            return {"ok": True, "message": "连接与登录成功（未发送任何邮件）",
                    "server_reply": str(code) + " " + str(msg)[:80]}
        except Exception as e:
            return {"ok": False, "message": redact("%s: %s" % (type(e).__name__, str(e)[:220]),
                                                   [s.get("_credential", "")])}

    def _message(self, subject: str, text_body: str, html_body: str, s: dict) -> EmailMessage:
        m = EmailMessage()
        m["Subject"] = subject
        m["From"] = formataddr((s.get("from_name") or "黄金积存金助手", s.get("user") or ""))
        to = [x for x in (s.get("to") or []) if x]
        cc = [x for x in (s.get("cc") or []) if x]
        if not to:
            raise RuntimeError("收件人为空")
        m["To"] = ", ".join(to)
        if cc:
            m["Cc"] = ", ".join(cc)
        m["Message-ID"] = make_msgid(domain="golddesk.local")
        m.set_content(text_body or "", subtype="plain", charset="utf-8")
        if s.get("html_and_text", True) and html_body:
            m.add_alternative(html_body, subtype="html", charset="utf-8")
        return m, to + cc

    def send_test_mail(self, override: dict | None = None) -> dict:
        s = self._settings(override)
        subject = "【黄金积存金助手】测试邮件 · %s" % bj_str(now_bj(), "%Y-%m-%d %H:%M")
        text = ("这是一封配置测试邮件，不是业务快报，不含任何行情数值或投资判断。\n"
                "发送端：%s 独立实例 %s\n时间（北京）：%s\n\n%s" %
                (self.cfg_store.get().get("app_kind"), self.cfg_store.instance_id, iso_bj(), DISCLAIMER))
        html = ('<div style="font-family:sans-serif;font-size:14px"><h3>配置测试邮件</h3>'
                '<p>这是一封配置测试邮件，不是业务快报，不含任何行情数值或投资判断。</p>'
                '<p style="color:#5B6472;font-size:12px">发送端：%s 独立实例 %s ｜ 北京时间 %s</p>'
                '<p style="font-size:11px;color:#5B6472">%s</p></div>') % (
                   self.cfg_store.get().get("app_kind"), self.cfg_store.instance_id, iso_bj(), DISCLAIMER)
        return self._deliver(subject, text, html, s, report_id=None, kind="test")

    def send_report(self, report: dict) -> dict:
        s = self._settings()
        if not s.get("enabled"):
            return {"ok": False, "skipped": True, "message": "邮件通道未启用（设置页可开启）"}
        if self.db.mails_today() >= int(s.get("max_mails_per_day", 12)):
            return {"ok": False, "skipped": True, "message": "已达本端单日邮件上限"}
        return self._deliver(report["subject"], report["text_body"], report["html"], s,
                             report_id=report.get("id"), kind="report")

    def _deliver(self, subject: str, text_body: str, html_body: str, s: dict,
                 report_id: int | None, kind: str) -> dict:
        cred = s.get("_credential", "")
        try:
            msg, recipients = self._message(subject, text_body, html_body, s)
        except Exception as e:
            return {"ok": False, "message": redact(str(e), [cred])}
        mid = msg["Message-ID"]
        attempts = int(s.get("retry_times") or 3)
        last = ""
        for i in range(max(1, attempts)):
            try:
                cli = self._connect(s)
                cli.send_message(msg, to_addrs=recipients)
                cli.quit()
                self.db.insert_delivery({"message_id": mid, "report_id": report_id, "channel": "mail",
                                         "recipient": ",".join(recipients), "status": "ok",
                                         "attempt_count": i + 1, "error": "", "sent_at": iso_bj()})
                self.db.event("info", "%s 邮件投递成功 %s" % (kind, subject[:60]))
                self.log.info("mail sent | %s | attempt=%d", subject[:60], i + 1)
                return {"ok": True, "message_id": mid, "attempt": i + 1,
                        "message": "投递成功（含 HTML + 纯文本双版本）"}
            except Exception as e:
                last = redact("%s: %s" % (type(e).__name__, str(e)[:220]), [cred])
                if i < attempts - 1:
                    time.sleep(1.5 * (2 ** i))
        self.db.insert_delivery({"message_id": mid, "report_id": report_id, "channel": "mail",
                                 "recipient": ",".join(s.get("to") or []), "status": "failed",
                                 "attempt_count": attempts, "error": last, "sent_at": iso_bj()})
        self.db.event("error", "邮件投递失败：" + last[:200])
        self.log.warning("mail failed | %s", last)
        # 失败时把 HTML 落盘，保证内容不丢失（FR-09）
        try:
            import os
            out = os.path.join(self.cfg_store.data_dir, "failed_mail_%s.html" %
                               bj_str(now_bj(), "%Y%m%d_%H%M%S"))
            with open(out, "w", encoding="utf-8") as f:
                f.write(html_body or "")
        except OSError:
            pass
        return {"ok": False, "message": "投递失败（已重试 %d 次并本地留痕）：%s" % (attempts, last)}
