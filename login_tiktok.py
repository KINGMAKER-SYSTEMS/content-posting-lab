#!/usr/bin/env python3
"""Run this to log into TikTok and save your session for the scraper."""
import asyncio
from scraper.tiktok_scraper import login_and_save_session

if __name__ == "__main__":
    asyncio.run(login_and_save_session())
