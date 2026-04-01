"""
InvestPilot - Security Alerts
E-Mail-Benachrichtigungen bei Sicherheitsvorfaellen.
"""

import os
import smtplib
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime

log = logging.getLogger("Alerts")

SMTP_SERVER = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_EMAIL = os.environ.get("SMTP_EMAIL", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
ALERT_RECIPIENT = os.environ.get("ALERT_RECIPIENT", "")


def send_security_alert(subject: str, body: str):
    """Sende Security-Alert per E-Mail."""
    if not SMTP_EMAIL or not SMTP_PASSWORD or not ALERT_RECIPIENT:
        log.warning(f"E-Mail nicht konfiguriert - Alert nur geloggt: {subject}")
        log.warning(f"  Detail: {body}")
        return False

    try:
        msg = MIMEMultipart()
        msg["From"] = SMTP_EMAIL
        msg["To"] = ALERT_RECIPIENT
        msg["Subject"] = f"[InvestPilot Security] {subject}"

        html = f"""
        <html>
        <body style="font-family: sans-serif; background: #1a1d2e; color: #e2e8f0; padding: 20px;">
            <div style="max-width: 500px; margin: 0 auto; background: #252839; border-radius: 12px; padding: 24px; border: 1px solid #ef4444;">
                <h2 style="color: #ef4444; margin-top: 0;">Security Alert</h2>
                <p style="font-size: 16px;">{subject}</p>
                <div style="background: #1a1d2e; padding: 16px; border-radius: 8px; margin: 16px 0;">
                    <pre style="white-space: pre-wrap; color: #94a3b8;">{body}</pre>
                </div>
                <p style="color: #94a3b8; font-size: 12px;">
                    Zeitpunkt: {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}<br>
                    InvestPilot Security System
                </p>
            </div>
        </body>
        </html>
        """
        msg.attach(MIMEText(html, "html"))

        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_EMAIL, SMTP_PASSWORD)
            server.send_message(msg)

        log.info(f"Security Alert gesendet: {subject}")
        return True

    except Exception as e:
        log.error(f"Alert E-Mail fehlgeschlagen: {e}")
        return False
