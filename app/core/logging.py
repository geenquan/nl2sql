import logging


def configure_logging(level: str = "INFO") -> None:
    # 禁止记录数据库密码、API Key 和未经脱敏的业务样本。
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
