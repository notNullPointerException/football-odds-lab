from urllib.parse import quote_plus

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # 数据库
    db_host: str = "localhost"
    db_port: int = 3306
    db_user: str = "root"
    db_password: str = ""
    db_name: str = "football"
    # DB_HOST=localhost 时走 Unix socket。MySQL 的 host 匹配无法区分
    # 本机 socket 和 127.0.0.1 TCP，只有 socket 才能在 skip_name_resolve
    # 打开后把管理账号和对外只读账号隔离开。
    db_unix_socket: str = "/tmp/mysql.sock"

    # API-Football
    api_football_key: str = ""
    api_football_base_url: str = ""

    # DeepSeek
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com/v1"
    deepseek_model: str = "deepseek-chat"

    # 缓存
    cache_ttl: int = 3600

    # Redis
    redis_host: str = "127.0.0.1"
    redis_port: int = 6379
    redis_db: int = 0
    redis_password: str = ""

    @property
    def db_url(self) -> str:
        user = quote_plus(self.db_user)
        password = quote_plus(self.db_password)
        if self.db_host == "localhost" and self.db_unix_socket:
            return (
                f"mysql+pymysql://{user}:{password}@/{self.db_name}"
                f"?unix_socket={quote_plus(self.db_unix_socket)}"
            )
        return (
            f"mysql+pymysql://{user}:{password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"


settings = Settings()
