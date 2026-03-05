import os
import logging
import resend

logger = logging.getLogger(__name__)

RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
FROM_EMAIL     = os.getenv("FROM_EMAIL", "noreply@radiateconvention.com")
FRONTEND_URL   = os.getenv("FRONTEND_URL", "https://goldfish-app-fuu3t.ondigitalocean.app")

resend.api_key = RESEND_API_KEY

# ── Shared design tokens ──────────────────────────────────────────────────────
_BG        = "#0a0a0a"
_CARD      = "#111111"
_CARD2     = "#1a1a1a"
_BORDER    = "#2a2a2a"
_ACCENT    = "#E3FC02"
_GREEN     = "#22c55e"
_TEXT      = "#f0f0f0"
_MUTED     = "#888888"
_FAINT     = "#555555"


def _logo_url() -> str:
    return f"{FRONTEND_URL}/assets/logo.png"


def _email_wrapper(body_html: str) -> str:
    """Wrap content in the full dark-themed outer shell with logo header."""
    logo = _logo_url()
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Radiate Portal</title>
</head>
<body style="margin:0;padding:0;background:{_BG};font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" role="presentation"
         style="background:{_BG};padding:32px 16px;">
    <tr>
      <td align="center">
        <!-- Card -->
        <table width="100%" cellpadding="0" cellspacing="0" role="presentation"
               style="max-width:560px;background:{_CARD};border:1px solid {_BORDER};border-radius:12px;overflow:hidden;">

          <!-- Header -->
          <tr>
            <td align="center"
                style="background:{_CARD};padding:28px 32px 20px;border-bottom:1px solid {_BORDER};">
              <img src="{logo}" alt="Radiate Portal"
                   width="64" height="64"
                   style="display:block;border-radius:10px;margin:0 auto 12px;" />
              <span style="color:{_ACCENT};font-size:18px;font-weight:800;
                           letter-spacing:.04em;text-transform:uppercase;">
                Radiate Portal
              </span>
            </td>
          </tr>

          <!-- Body -->
          {body_html}

          <!-- Footer -->
          <tr>
            <td style="padding:20px 32px 28px;border-top:1px solid {_BORDER};text-align:center;">
              <p style="margin:0 0 4px;color:{_FAINT};font-size:12px;">
                Questions? &nbsp;
                <a href="mailto:{FROM_EMAIL}"
                   style="color:{_ACCENT};text-decoration:none;">{FROM_EMAIL}</a>
              </p>
              <p style="margin:0;color:{_FAINT};font-size:11px;">
                &copy; Radiate Portal &mdash; All rights reserved.
              </p>
            </td>
          </tr>

        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""


def _btn(url: str, label: str) -> str:
    return (
        f'<a href="{url}" target="_blank"'
        f' style="display:inline-block;background:{_ACCENT};color:#000;'
        f'font-weight:700;font-size:15px;padding:13px 28px;border-radius:7px;'
        f'text-decoration:none;letter-spacing:.01em;">'
        f'{label}</a>'
    )


def _send(to_email: str, subject: str, html: str) -> None:
    resend.Emails.send({
        "from": FROM_EMAIL,
        "to": [to_email],
        "subject": subject,
        "html": html,
    })


# ── Emails ────────────────────────────────────────────────────────────────────

def send_verification_email(to_email: str, token: str) -> None:
    """Send an account-verification email."""
    url  = f"{FRONTEND_URL}/verify-email.html?token={token}"
    body = f"""
          <tr>
            <td style="padding:32px 32px 8px;">
              <h2 style="margin:0 0 12px;color:{_TEXT};font-size:20px;font-weight:700;">
                Verify your email
              </h2>
              <p style="margin:0 0 28px;color:{_MUTED};font-size:15px;line-height:1.6;">
                Thanks for signing up. Click the button below to verify your
                email address and activate your account.
              </p>
              <p style="margin:0 0 28px;">{_btn(url, "Verify Email")}</p>
              <p style="margin:0;color:{_FAINT};font-size:12px;line-height:1.6;">
                This link expires in 24&nbsp;hours. If you did not create an
                account, you can safely ignore this email.
              </p>
            </td>
          </tr>
          <tr>
            <td style="padding:0 32px 28px;">
              <p style="margin:0;color:{_FAINT};font-size:11px;
                         background:{_CARD2};border:1px solid {_BORDER};
                         border-radius:6px;padding:10px 12px;
                         word-break:break-all;">
                {url}
              </p>
            </td>
          </tr>"""
    _send(to_email, "Verify your Radiate Portal account", _email_wrapper(body))


def send_reset_email(to_email: str, token: str) -> None:
    """Send a password-reset email."""
    url  = f"{FRONTEND_URL}/reset-password.html?token={token}"
    body = f"""
          <tr>
            <td style="padding:32px 32px 8px;">
              <h2 style="margin:0 0 12px;color:{_TEXT};font-size:20px;font-weight:700;">
                Reset your password
              </h2>
              <p style="margin:0 0 28px;color:{_MUTED};font-size:15px;line-height:1.6;">
                We received a request to reset your password. Click the button
                below to choose a new one.
              </p>
              <p style="margin:0 0 28px;">{_btn(url, "Reset Password")}</p>
              <p style="margin:0;color:{_FAINT};font-size:12px;line-height:1.6;">
                This link expires in 1&nbsp;hour. If you did not request a
                password reset, you can safely ignore this email.
              </p>
            </td>
          </tr>
          <tr>
            <td style="padding:0 32px 28px;">
              <p style="margin:0;color:{_FAINT};font-size:11px;
                         background:{_CARD2};border:1px solid {_BORDER};
                         border-radius:6px;padding:10px 12px;
                         word-break:break-all;">
                {url}
              </p>
            </td>
          </tr>"""
    _send(to_email, "Reset your Radiate Portal password", _email_wrapper(body))


def send_registration_confirmation(
    to_email: str,
    studio_name: str | None,
    event_title: str,
    event_date: str,
    student_names: list[str],
    amount_paid: float,
    observer_names: list[str] | None = None,
    observer_amount: float = 0,
) -> None:
    """Send a registration confirmation / receipt email after finalizing."""
    display_name = studio_name or "Independent Dancer"
    amount_str   = "Free" if amount_paid == 0 else f"${amount_paid:.2f}"
    amount_color = _GREEN if amount_paid == 0 else _ACCENT

    def _detail_row(label: str, value: str, value_style: str = "") -> str:
        return f"""
          <tr>
            <td style="padding:11px 0;color:{_MUTED};font-size:13px;
                        width:38%;vertical-align:top;border-bottom:1px solid {_BORDER};">
              {label}
            </td>
            <td style="padding:11px 0;color:{_TEXT};font-size:14px;font-weight:600;
                        vertical-align:top;border-bottom:1px solid {_BORDER};
                        {value_style}">
              {value}
            </td>
          </tr>"""

    student_rows = "".join(
        f"""<tr>
              <td style="padding:9px 12px;color:{_TEXT};font-size:14px;
                          border-bottom:1px solid {_BORDER};">
                <span style="color:{_ACCENT};margin-right:8px;">&#8227;</span>{name}
              </td>
            </tr>"""
        for name in student_names
    )

    observer_section = ""
    if observer_names:
        obs_amount_str = "Free" if observer_amount == 0 else f"${observer_amount:.2f}"
        observer_rows_html = "".join(
            f'<tr><td style="padding:9px 12px;color:{_TEXT};font-size:14px;border-bottom:1px solid {_BORDER};">'
            f'<span style="color:#a78bfa;margin-right:8px;">&#8227;</span>{name}</td></tr>'
            for name in observer_names
        )
        observer_section = f"""
          <tr>
            <td style="padding:0 32px 28px;">
              <p style="margin:0 0 10px;color:{_MUTED};font-size:11px;font-weight:700;
                         text-transform:uppercase;letter-spacing:.08em;">
                Registered Observers &nbsp;
                <span style="color:#a78bfa;font-size:11px;">({obs_amount_str})</span>
              </p>
              <table width="100%" cellpadding="0" cellspacing="0" role="presentation"
                     style="background:{_CARD2};border:1px solid {_BORDER};border-radius:8px;">
                {observer_rows_html}
              </table>
            </td>
          </tr>"""

    body = f"""
          <!-- Green confirmation banner -->
          <tr>
            <td style="background:rgba(34,197,94,.12);border-bottom:1px solid rgba(34,197,94,.25);
                        padding:14px 32px;text-align:center;">
              <span style="color:{_GREEN};font-size:22px;font-weight:800;
                           letter-spacing:.01em;">
                &#10003;&nbsp; Registration Confirmed
              </span>
            </td>
          </tr>

          <!-- Intro -->
          <tr>
            <td style="padding:28px 32px 4px;">
              <p style="margin:0;color:{_MUTED};font-size:14px;line-height:1.6;">
                Your registration has been finalized. A summary of your booking
                is below &mdash; keep this email for your records.
              </p>
            </td>
          </tr>

          <!-- Details card -->
          <tr>
            <td style="padding:16px 32px;">
              <table width="100%" cellpadding="0" cellspacing="0" role="presentation"
                     style="background:{_CARD2};border:1px solid {_BORDER};
                             border-radius:8px;padding:0 16px;">
                {_detail_row("Studio / Dancer", display_name)}
                {_detail_row("Event", event_title)}
                {_detail_row("Event Date", event_date)}
                {_detail_row("Amount Paid",
                             amount_str,
                             f"color:{amount_color};font-size:18px;font-weight:800;")}
              </table>
            </td>
          </tr>

          <!-- Dancers section -->
          <tr>
            <td style="padding:0 32px 28px;">
              <p style="margin:0 0 10px;color:{_MUTED};font-size:11px;font-weight:700;
                         text-transform:uppercase;letter-spacing:.08em;">
                Registered Dancers
              </p>
              <table width="100%" cellpadding="0" cellspacing="0" role="presentation"
                     style="background:{_CARD2};border:1px solid {_BORDER};border-radius:8px;">
                {student_rows}
              </table>
            </td>
          </tr>
          {observer_section}"""

    _send(
        to_email,
        f"\u2713 Registration Confirmed \u2014 {event_title}",
        _email_wrapper(body),
    )
