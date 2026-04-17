import subprocess
import os
import sys
from loguru import logger


def main():
    logger.info("🌡️ PolyWeather weather query bot starting...")

    # Create data directory
    os.makedirs("data", exist_ok=True)

    # Run bot_listener directly
    cmd = [sys.executable, "bot_listener.py"]
    logger.success("🚀 Online! Awaiting Telegram commands...")

    try:
        subprocess.run(cmd, cwd=os.getcwd())
    except KeyboardInterrupt:
        logger.warning("Stopping...")


if __name__ == "__main__":
    main()
