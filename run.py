import logging

from core.bot import StarbrightBot
from core.config import Config


def main() -> None:
    """Load configuration and run the bot until it disconnects."""
    logging.basicConfig(level=logging.INFO)
    config = Config.from_env()
    bot = StarbrightBot(config)
    bot.run(config.discord_token, log_handler=None)


if __name__ == "__main__":
    main()
