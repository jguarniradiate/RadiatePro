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


def send_registration_confirmation(
    to_email: str,
    studio_name: str | None,
    event_title: str,
    event_date: str,
    student_names: list[str],
    amount_paid: float,
) -> None:
    """Send a registration confirmation email after a user finalizes and pays."""
    display_name = studio_name or "Independent Dancer"
    amount_str   = "Free" if amount_paid == 0 else f"${amount_paid:.2f}"

    student_rows = "".join(
        f'<tr><td style="padding:6px 0;border-bottom:1px solid #1e1e1e;color:#f0f0f0;">{name}</td></tr>'
        for name in student_names
    )

    html = f"""
    <div style="{_BASE_STYLE}">
      <h2 style="color:#E3FC02;margin-top:0;">Registration Confirmed</h2>
      <p style="color:#ccc;">Your registration has been finalized. See the details below.</p>

      <table style="width:100%;border-collapse:collapse;margin-bottom:24px;">
        <tr>
          <td style="padding:8px 0;color:#888;font-size:13px;width:40%;">Studio / Dancer</td>
          <td style="padding:8px 0;color:#f0f0f0;font-weight:600;">{display_name}</td>
        </tr>
        <tr>
          <td style="padding:8px 0;color:#888;font-size:13px;">Event</td>
          <td style="padding:8px 0;color:#f0f0f0;font-weight:600;">{event_title}</td>
        </tr>
        <tr>
          <td style="padding:8px 0;color:#888;font-size:13px;">Event Date</td>
          <td style="padding:8px 0;color:#f0f0f0;">{event_date}</td>
        </tr>
        <tr>
          <td style="padding:8px 0;color:#888;font-size:13px;">Amount Paid</td>
          <td style="padding:8px 0;color:#E3FC02;font-weight:700;font-size:16px;">{amount_str}</td>
        </tr>
      </table>

      <p style="color:#888;font-size:13px;margin-bottom:8px;text-transform:uppercase;letter-spacing:.05em;">Registered Students</p>
      <table style="width:100%;border-collapse:collapse;margin-bottom:24px;">
        {student_rows}
      </table>

      <p style="color:#555;font-size:12px;margin-top:32px;">
        Questions? Contact us at <a href="mailto:{FROM_EMAIL}" style="color:#E3FC02;">{FROM_EMAIL}</a>
      </p>
    </div>
    """
    _send(to_email, f"Registration Confirmed — {event_title}", html)


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
