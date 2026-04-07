"""
One-time script to generate a Pyrogram session string.

Run locally:
    pip install pyrotgfork
    python generate_session.py

Enter your phone number and OTP when prompted.
Copy the session string and set it as TELEGRAM_SESSION_STRING on Railway.
"""

import asyncio
from pyrogram import Client


async def main():
    api_id = input("Enter your TELEGRAM_API_ID: ").strip()
    api_hash = input("Enter your TELEGRAM_API_HASH: ").strip()

    app = Client(
        name="cpl_session_gen",
        api_id=int(api_id),
        api_hash=api_hash,
        in_memory=True,
    )

    async with app:
        session_string = await app.export_session_string()
        print("\n" + "=" * 60)
        print("SESSION STRING (copy this entire value):")
        print("=" * 60)
        print(session_string)
        print("=" * 60)
        print("\nSet this as TELEGRAM_SESSION_STRING on Railway.")


if __name__ == "__main__":
    asyncio.run(main())
