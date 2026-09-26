#!/usr/bin/env python3
"""Render every transactional email to files for review, or send them to yourself.

    python scripts/preview_emails.py                      # writes build/email-previews/<name>.html and .txt
    python scripts/preview_emails.py --out /tmp/previews
    REMEMBRA_RESEND_API_KEY=... python scripts/preview_emails.py --send-to you@example.com

Rendering needs no network and no key. ``--send-to`` sends every sample
through Resend to that one address (a staging check of delivery, the plain
text part and Reply-To); it never sends anywhere else.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from remembra.cloud.email import email_service_or_none  # noqa: E402
from remembra.cloud.email_templates import sample_renders  # noqa: E402


async def _send_all(to: str) -> int:
    service = email_service_or_none()
    if service is None:
        print("REMEMBRA_RESEND_API_KEY is not set: nothing sent.", file=sys.stderr)
        return 1
    failures = 0
    for name, email in sample_renders(service.dashboard).items():
        result = await service.send_rendered(to, email)
        print(f"{name}: {'sent ' + (result.message_id or '') if result.success else 'FAILED ' + str(result.error)}")
        failures += not result.success
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default="build/email-previews", help="directory for the rendered files")
    parser.add_argument("--send-to", help="also send every sample to this one address (needs a Resend key)")
    args = parser.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for name, email in sample_renders().items():
        (out / f"{name}.html").write_text(email.html, encoding="utf-8")
        (out / f"{name}.txt").write_text(f"Subject: {email.subject}\n\n{email.text}", encoding="utf-8")
        print(f"{name}: {email.subject}")
    print(f"wrote {out}/")
    if args.send_to:
        return asyncio.run(_send_all(args.send_to))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
