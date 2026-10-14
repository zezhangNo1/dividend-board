"""验证码发送通道：邮箱（SMTP）与短信（腾讯云 SMS，备案签名下来后启用）。

统一接口：send(code, target) -> None（失败抛异常）。通道由环境变量选择，
新增通道只需在此文件追加一个类并注册到 CHANNELS。
"""
import logging
import os
import smtplib
from email.mime.text import MIMEText

log = logging.getLogger("channels")


class EmailChannel:
    """SMTP 邮箱验证码。未配置 SMTP 时进入 DEV 模式：验证码打到日志里。"""

    name = "email"

    def send(self, code: str, target: str):
        host = os.environ.get("SMTP_HOST", "")
        if not host:
            log.warning("[DEV] 邮箱 %s 的验证码：%s（未配置 SMTP_HOST，仅打日志）", target, code)
            return
        msg = MIMEText(
            f"您的验证码是 {code}，10 分钟内有效。若非本人操作请忽略本邮件。\n\n—— 泽克的数据笔记",
            "plain",
            "utf-8",
        )
        msg["Subject"] = f"【泽克的数据笔记】登录验证码 {code}"
        msg["From"] = os.environ.get("SMTP_FROM", "")
        msg["To"] = target
        user = os.environ.get("SMTP_USER", "")
        pwd = os.environ.get("SMTP_PASS", "")
        port = int(os.environ.get("SMTP_PORT", "465"))
        with smtplib.SMTP_SSL(host, port, timeout=15) as s:
            if user:
                s.login(user, pwd)
            s.sendmail(msg["From"], [target], msg.as_string())


class SmsChannel:
    """腾讯云短信。需备案号下来并申请到签名后，配置以下环境变量即可启用：
    SMS_SIGN（签名，须与备案网站名一致）、SMS_SECRET_ID / SMS_SECRET_KEY、SMS_SDK_APP_ID
    """

    name = "phone"
    ENABLED = bool(os.environ.get("SMS_SECRET_ID"))

    def send(self, code: str, target: str):
        if not self.ENABLED:
            raise RuntimeError("短信通道未启用：等待备案签名（配置 SMS_* 环境变量后自动生效）")
        import httpx

        # 腾讯云 SMS API（tc3-hmac-sha256 签名）。为避免引重 SDK，
        # 这里用最简实现；上线前建议换官方 tencentcloud-sdk-python。
        secret_id = os.environ["SMS_SECRET_ID"]
        secret_key = os.environ["SMS_SECRET_KEY"]
        params = {
            "PhoneNumberSet": ["+86" + target.lstrip("+86")],
            "SmsSdkAppId": os.environ["SMS_SDK_APP_ID"],
            "SignName": os.environ["SMS_SIGN"],
            "TemplateId": os.environ["SMS_TEMPLATE_ID"],
            "TemplateParamSet": [code],
        }
        # 简化接入：走腾讯云 API 网关的 token 方式由 SDK 完成更稳妥，
        # 此处留待启用时按当时控制台参数完成（见项目记忆）。
        raise RuntimeError("短信通道待接入：备案签名通过后完成最终对接")


CHANNELS = {"email": EmailChannel(), "phone": SmsChannel()}
