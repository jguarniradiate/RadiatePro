import os
import logging
import resend

logger = logging.getLogger(__name__)

RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
FROM_EMAIL     = os.getenv("FROM_EMAIL", "noreply@radiateconvention.com")
FRONTEND_URL   = os.getenv("FRONTEND_URL", "https://goldfish-app-fuu3t.ondigitalocean.app")

resend.api_key = RESEND_API_KEY

_BASE_STYLE = """
  font-family: sans-serif;
  max-width: 520px;
  margin: auto;
  background: #000;
  color: #f0f0f0;
  padding: 32px;
  border-radius: 8px;
"""

_BTN_STYLE = (
    "background:#E3FC02;color:#000;font-weight:700;"
    "padding:12px 24px;border-radius:6px;"
    "text-decoration:none;display:inline-block;"
)


def _send(to_email: str, subject: str, html: str) -> None:
    resend.Emails.send({
        "from": FROM_EMAIL,
        "to": [to_email],
        "subject": subject,
        "html": html,
    })


def send_verification_email(to_email: str, token: str) -> None:
    """Send an account-verification email."""
    url = f"{FRONTEND_URL}/verify-email.html?token={token}"
    html = f"""
    <div style="{_BASE_STYLE}">
      <h2 style="color:#E3FC02;margin-top:0;">RadiatePro</h2>
      <p>Thanks for signing up. Click the button below to verify your email address.</p>
      <p style="margin:32px 0;">
        <a href="{url}" style="{_BTN_STYLE}">Verify Email</a>
      </p>
      <p style="color:#888;font-size:13px;">
        This link expires in 24&nbsp;hours. If you did not create an account, ignore this email.
      </p>
      <p style="color:#555;font-size:12px;">Or paste this URL into your browser:<br>{url}</p>
    </div>
    """
    _send(to_email, "Verify your RadiatePro account", html)


def send_reset_email(to_email: str, token: str) -> None:
    """Send a password-reset email."""
    url = f"{FRONTEND_URL}/reset-password.html?token={token}"
    html = f"""
    <div style="{_BASE_STYLE}">
      <h2 style="color:#E3FC02;margin-top:0;">RadiatePro</h2>
      <p>We received a request to reset your password. Click the button below to choose a new one.</p>
      <p style="margin:32px 0;">
        <a href="{url}" style="{_BTN_STYLE}">Reset Password</a>
      </p>
      <p style="color:#888;font-size:13px;">
        This link expires in 1&nbsp;hour. If you did not request a reset, ignore this email.
      </p>
      <p style="color:#555;font-size:12px;">Or paste this URL into your browser:<br>{url}</p>
    </div>
    """
    _send(to_email, "Reset your RadiatePro password", html)
