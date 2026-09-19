import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass
class Config:
    """Runtime configuration for the bot, loaded from the environment."""

    discord_token: str
    command_prefix: str
    postgres_dsn: str
    ns_user_agent: str

    @classmethod
    def from_env(cls) -> "Config":
        """Load configuration from environment variables, reading a .env file first if present.
        :return: populated Config instance
        """
        load_dotenv()
        return cls(
            discord_token=os.environ["DISCORD_TOKEN"],
            command_prefix=os.environ.get("COMMAND_PREFIX", "!"),
            postgres_dsn=os.environ["POSTGRES_DSN"],
            ns_user_agent=os.environ["NS_USER_AGENT"],
        )
