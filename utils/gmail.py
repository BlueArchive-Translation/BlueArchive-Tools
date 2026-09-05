import os
import ssl
import mimetypes
import smtplib

from pathlib import Path
from email.mime.base import MIMEBase
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
from email.mime.image import MIMEImage
from email.mime.audio import MIMEAudio
from email import encoders
from email.header import Header
from email.utils import formatdate, make_msgid


class GmailSMTP:
    SMTP_SERVER = "smtp.gmail.com"
    SMTP_PORT = 587
    SMTP_SSL_PORT = 465

    def __init__(
        self,
        email=None,
        password=None,
        smtp_server=None,
        smtp_port=None,
        use_ssl=False,
        timeout=30,
        debug=False
    ):
        self.email = email or os.getenv("GMAIL_EMAIL") or os.getenv("EMAIL_USERNAME")
        self.password = password or os.getenv("GMAIL_PASSWORD") or os.getenv("EMAIL_PASSWORD")
        self.smtp_server = smtp_server or self.SMTP_SERVER
        self.use_ssl = use_ssl
        self.smtp_port = smtp_port or (
            self.SMTP_SSL_PORT if use_ssl else self.SMTP_PORT
        )
        self.timeout = timeout
        self.debug = debug
        self.server = None

        if not self.email:
            raise ValueError("未配置 Gmail 邮箱地址")
        if not self.password:
            raise ValueError("未配置 Gmail App Password")

    def connect(self):
        if self.server:
            return self

        print("正在连接 Gmail SMTP...")

        if self.use_ssl:
            context = ssl.create_default_context()
            self.server = smtplib.SMTP_SSL(
                self.smtp_server,
                self.smtp_port,
                timeout=self.timeout,
                context=context
            )
            if self.debug:
                self.server.set_debuglevel(1)
            self.server.ehlo()
        else:
            self.server = smtplib.SMTP(
                self.smtp_server,
                self.smtp_port,
                timeout=self.timeout
            )
            if self.debug:
                self.server.set_debuglevel(1)
            self.server.ehlo()
            print("SMTP 连接成功")
            context = ssl.create_default_context()
            self.server.starttls(context=context)
            self.server.ehlo()
            print("TLS 加密成功")

        self.server.login(self.email, self.password)
        print("Gmail 登录成功")
        return self

    def disconnect(self):
        if not self.server:
            return

        try:
            self.server.quit()
        except Exception:
            try:
                self.server.close()
            except Exception:
                pass

        self.server = None

    def __enter__(self):
        return self.connect()

    def __exit__(self, exc_type, exc_value, traceback):
        self.disconnect()

    @staticmethod
    def _normalize_recipients(value):
        if not value:
            return []

        if isinstance(value, str):
            return [
                item.strip()
                for item in value.split(",")
                if item.strip()
            ]

        return [
            str(item).strip()
            for item in value
            if str(item).strip()
        ]

    @staticmethod
    def _header(value):
        if value is None:
            return None
        return str(Header(str(value), "utf-8"))

    def create_message(
        self,
        to,
        subject,
        html=None,
        text=None,
        cc=None,
        bcc=None,
        reply_to=None,
        from_name=None,
        attachments=None,
        inline_images=None,
        headers=None
    ):
        to = self._normalize_recipients(to)
        cc = self._normalize_recipients(cc)
        bcc = self._normalize_recipients(bcc)

        if not to and not cc and not bcc:
            raise ValueError("至少需要一个收件人")

        if html is not None and text is not None:
            message = MIMEMultipart("alternative")
            message.attach(MIMEText(text, "plain", "utf-8"))
            message.attach(MIMEText(html, "html", "utf-8"))
        elif html is not None:
            message = MIMEMultipart("alternative")
            message.attach(MIMEText(html, "html", "utf-8"))
        else:
            message = MIMEText(text or "", "plain", "utf-8")

        if attachments or inline_images:
            if message.get_content_maintype() == "multipart":
                body = message
            else:
                body = MIMEMultipart("mixed")
                body.attach(message)
                message = body

        message["From"] = (
            f"{self._header(from_name)} <{self.email}>"
            if from_name
            else self.email
        )
        message["To"] = ", ".join(to)

        if cc:
            message["Cc"] = ", ".join(cc)

        message["Subject"] = self._header(subject)
        message["Date"] = formatdate(localtime=True)
        message["Message-ID"] = make_msgid(domain="gmail.com")

        if reply_to:
            message["Reply-To"] = ", ".join(
                self._normalize_recipients(reply_to)
            )

        if headers:
            for key, value in headers.items():
                if key.lower() in {
                    "from",
                    "to",
                    "cc",
                    "bcc",
                    "subject",
                    "date",
                    "message-id"
                }:
                    continue
                message[key] = self._header(value)

        if inline_images:
            for image in inline_images:
                self._attach_file(message, image, inline=True)

        if attachments:
            for attachment in attachments:
                self._attach_file(message, attachment)

        return message

    def _attach_file(self, message, file_path, inline=False):
        file_path = Path(file_path)

        if not file_path.is_file():
            raise FileNotFoundError(f"附件不存在: {file_path}")

        filename = file_path.name
        mime_type, _ = mimetypes.guess_type(str(file_path))
        mime_type = mime_type or "application/octet-stream"

        with open(file_path, "rb") as f:
            data = f.read()

        maintype, subtype = mime_type.split("/", 1)

        if inline and maintype == "image":
            part = MIMEImage(data, _subtype=subtype)
            part.add_header(
                "Content-Disposition",
                "inline",
                filename=filename
            )
            part.add_header(
                "Content-ID",
                f"<{filename}>"
            )
        elif maintype == "image":
            part = MIMEImage(data, _subtype=subtype)
            part.add_header(
                "Content-Disposition",
                "attachment",
                filename=("utf-8", "", filename)
            )
        elif maintype == "audio":
            part = MIMEAudio(data, _subtype=subtype)
            part.add_header(
                "Content-Disposition",
                "attachment",
                filename=("utf-8", "", filename)
            )
        elif maintype == "application":
            part = MIMEApplication(data, _subtype=subtype)
            part.add_header(
                "Content-Disposition",
                "attachment",
                filename=("utf-8", "", filename)
            )
        else:
            part = MIMEBase(maintype, subtype)
            part.set_payload(data)
            encoders.encode_base64(part)
            part.add_header(
                "Content-Disposition",
                "attachment",
                filename=("utf-8", "", filename)
            )

        message.attach(part)

    def send(
        self,
        to,
        subject,
        html=None,
        text=None,
        cc=None,
        bcc=None,
        reply_to=None,
        from_name=None,
        attachments=None,
        inline_images=None,
        headers=None
    ):
        message = self.create_message(
            to=to,
            subject=subject,
            html=html,
            text=text,
            cc=cc,
            bcc=bcc,
            reply_to=reply_to,
            from_name=from_name,
            attachments=attachments,
            inline_images=inline_images,
            headers=headers
        )

        recipients = (
            self._normalize_recipients(to)
            + self._normalize_recipients(cc)
            + self._normalize_recipients(bcc)
        )

        if not self.server:
            self.connect()

        print("正在发送邮件...")

        result = self.server.sendmail(
            self.email,
            recipients,
            message.as_string()
        )

        if result:
            print("邮件发送失败，服务器拒绝了部分收件人:")
            print(result)
        else:
            print("SMTP 已接受邮件。")

        return {
            "success": not bool(result),
            "result": result,
            "message_id": message["Message-ID"],
            "from": self.email,
            "to": self._normalize_recipients(to),
            "cc": self._normalize_recipients(cc),
            "bcc": self._normalize_recipients(bcc)
        }

    def send_html(self, to, subject, html, text=None, **kwargs):
        return self.send(
            to=to,
            subject=subject,
            html=html,
            text=text,
            **kwargs
        )

    def send_text(self, to, subject, text, **kwargs):
        return self.send(
            to=to,
            subject=subject,
            text=text,
            **kwargs
        )

    @staticmethod
    def update_html(server, run_id):
        return f"""
<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>BA 更新提醒</title>
</head>
<body style="margin:0;padding:0;background:#f3f5f9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,'Microsoft YaHei',sans-serif;color:#1f2937;">
<table width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#f3f5f9;">
<tr>
<td align="center" style="padding:45px 15px;">
<table width="600" cellpadding="0" cellspacing="0" border="0" style="width:100%;max-width:600px;background:#ffffff;border-radius:20px;overflow:hidden;box-shadow:0 8px 30px rgba(0,0,0,.06);">

<tr>
<td style="padding:34px 38px;background:linear-gradient(135deg,#667eea,#764ba2);">
<table width="100%" cellpadding="0" cellspacing="0" border="0">
<tr>
<td>
<div style="font-size:12px;letter-spacing:2px;color:#ffffff;opacity:.75;font-weight:bold;">
BLUE ARCHIVE
</div>
<div style="font-size:28px;line-height:1.3;font-weight:bold;color:#ffffff;margin-top:10px;">
⚠️ BA 更新提醒
</div>
<div style="font-size:14px;line-height:1.6;color:#ffffff;opacity:.9;margin-top:6px;">
检测到新的版本变更
</div>
</td>
<td width="70" align="right" valign="middle">
<div style="width:58px;height:58px;line-height:58px;text-align:center;background:rgba(255,255,255,.16);border-radius:16px;font-size:28px;">
🔔
</div>
</td>
</tr>
</table>
</td>
</tr>

<tr>
<td style="padding:35px 38px 20px;">

<div style="font-size:13px;color:#9ca3af;font-weight:bold;letter-spacing:1px;">
SERVER
</div>

<div style="margin-top:10px;padding:16px 20px;background:#f6f7fb;border:1px solid #eaecf2;border-radius:14px;">
<div style="font-size:22px;font-weight:bold;color:#374151;">
{server}
</div>
<div style="font-size:13px;color:#9ca3af;margin-top:4px;">
版本更新已检测
</div>
</div>

<div style="margin-top:25px;font-size:15px;line-height:1.9;color:#5b6472;">
检测到 <strong style="color:#667eea;">{server}</strong> 发生版本更新。
<br>
目前正在部署自动化处理，稍后将发送处理进度。
</div>

<table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-top:25px;background:#f8f9fc;border-radius:14px;">
<tr>
<td style="padding:18px 20px;">
<div style="font-size:12px;color:#9ca3af;letter-spacing:.5px;">
TRIGGER RUN ID
</div>
<div style="font-size:15px;font-weight:bold;color:#374151;margin-top:5px;">
{run_id}
</div>
</td>
</tr>
</table>

</td>
</tr>

<tr>
<td style="padding:15px 38px 35px;">
<div style="padding:15px 18px;background:#eef2ff;border-radius:12px;color:#667eea;font-size:13px;line-height:1.7;">
💡 <strong>自动化处理中</strong><br>
系统正在自动处理本次版本更新，请稍候。
</div>
</td>
</tr>

<tr>
<td style="padding:20px 38px;background:#fafafa;border-top:1px solid #f0f0f0;">
<table width="100%" cellpadding="0" cellspacing="0" border="0">
<tr>
<td style="font-size:12px;color:#9ca3af;">
北辰汉化组
</td>
<td align="right" style="font-size:12px;color:#c0c4cc;">
Automated Notification
</td>
</tr>
</table>
</td>
</tr>

</table>
</td>
</tr>
</table>
</body>
</html>
"""

    @staticmethod
    def warning_html(repository, run_id):
        url = f"https://github.com/{repository}/actions/runs/{run_id}"

        return f"""
<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Restart 流程警告</title>
</head>
<body style="margin:0;padding:0;background:#f3f5f9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,'Microsoft YaHei',sans-serif;color:#1f2937;">
<table width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#f3f5f9;">
<tr>
<td align="center" style="padding:45px 15px;">

<table width="600" cellpadding="0" cellspacing="0" border="0" style="width:100%;max-width:600px;background:#ffffff;border-radius:20px;overflow:hidden;box-shadow:0 8px 30px rgba(0,0,0,.06);">

<tr>
<td style="padding:34px 38px;background:linear-gradient(135deg,#f97316,#dc2626);">
<table width="100%" cellpadding="0" cellspacing="0" border="0">
<tr>
<td>
<div style="font-size:12px;letter-spacing:2px;color:#ffffff;opacity:.75;font-weight:bold;">
BA AUTOMATION
</div>
<div style="font-size:28px;line-height:1.3;font-weight:bold;color:#ffffff;margin-top:10px;">
⚠️ 流程异常
</div>
<div style="font-size:14px;line-height:1.6;color:#ffffff;opacity:.9;margin-top:6px;">
Restart 流程已意外终止
</div>
</td>
<td width="70" align="right" valign="middle">
<div style="width:58px;height:58px;line-height:58px;text-align:center;background:rgba(255,255,255,.16);border-radius:16px;font-size:28px;color:#ffffff;">
!
</div>
</td>
</tr>
</table>
</td>
</tr>

<tr>
<td style="padding:35px 38px 20px;">

<div style="padding:20px;background:#fff7ed;border:1px solid #fed7aa;border-radius:14px;">
<div style="font-size:13px;color:#ea580c;font-weight:bold;letter-spacing:.5px;">
WARNING
</div>
<div style="font-size:21px;font-weight:bold;color:#9a3412;margin-top:8px;">
Restart 流程因未知原因崩溃
</div>
</div>

<div style="margin-top:25px;font-size:15px;line-height:1.9;color:#5b6472;">
检测到 Restart 流程因未知原因被终止。
<br>
您可以点击下方按钮查看 GitHub Actions 运行详情。
</div>

<table cellpadding="0" cellspacing="0" border="0" style="margin-top:28px;">
<tr>
<td style="border-radius:10px;background:#dc2626;">
<a href="{url}" style="display:inline-block;padding:15px 28px;color:#ffffff;text-decoration:none;font-size:15px;font-weight:bold;">
查看运行详情 →
</a>
</td>
</tr>
</table>

<table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-top:28px;background:#f8f9fc;border-radius:14px;">
<tr>
<td style="padding:18px 20px;">
<div style="font-size:12px;color:#9ca3af;letter-spacing:.5px;">
GITHUB RUN ID
</div>
<div style="font-size:15px;font-weight:bold;color:#374151;margin-top:5px;">
{run_id}
</div>
</td>
</tr>
</table>

</td>
</tr>

<tr>
<td style="padding:15px 38px 35px;">
<div style="padding:15px 18px;background:#fff7ed;border-radius:12px;color:#c2410c;font-size:13px;line-height:1.7;">
⚠️ <strong>需要检查</strong><br>
请查看 GitHub Actions 日志以确定具体失败原因。
</div>
</td>
</tr>

<tr>
<td style="padding:20px 38px;background:#fafafa;border-top:1px solid #f0f0f0;">
<table width="100%" cellpadding="0" cellspacing="0" border="0">
<tr>
<td style="font-size:12px;color:#9ca3af;">
北辰汉化组
</td>
<td align="right" style="font-size:12px;color:#c0c4cc;">
Automated Warning
</td>
</tr>
</table>
</td>
</tr>

</table>

</td>
</tr>
</table>
</body>
</html>
"""

    def send_update_notice(
        self,
        server,
        run_id,
        to=None,
        subject=None,
        from_name="北辰汉化组"
    ):
        to = to or os.getenv("TARGET_EMAIL")
        subject = subject or f"⚠️ BA更新提醒 - {server} 版本更新"

        return self.send_html(
            to=to,
            subject=subject,
            html=self.update_html(
                server=server,
                run_id=run_id
            ),
            text=(
                f"检测到 {server} 发生版本更新。\n"
                f"目前正在部署自动化处理，稍后将发送处理进度。\n\n"
                f"触发运行ID: {run_id}"
            ),
            from_name=from_name
        )

    def send_restart_warning(
        self,
        repository,
        run_id,
        to=None,
        subject="⚠️ 警告 - Restart流程因未知原因崩溃",
        from_name="北辰汉化组"
    ):
        to = to or os.getenv("TARGET_EMAIL")

        url = f"https://github.com/{repository}/actions/runs/{run_id}"

        return self.send_html(
            to=to,
            subject=subject,
            html=self.warning_html(
                repository=repository,
                run_id=run_id
            ),
            text=(
                "检测到Restart流程因未知原因被终止，"
                "您可以点击下方链接查看详情。\n\n"
                f"{url}"
            ),
            from_name=from_name
        )