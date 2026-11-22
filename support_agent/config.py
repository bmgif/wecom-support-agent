import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    """One test account and its local ledger. / 单个测试客服账号及其本地记录库。"""

    corp_id: str
    secret: str = field(repr=False)
    callback_token: str = field(repr=False)
    encoding_aes_key: str = field(repr=False)
    open_kfid: str
    db_path: Path

    @classmethod
    def from_env(cls) -> "Settings | None":
        """Load all channel settings together, or start in health-only mode.
        渠道配置必须完整提供。完全未配置时只启用健康检查。
        """
        names = (
            "WECOM_CORP_ID", "WECOM_KF_SECRET", "WECOM_CALLBACK_TOKEN",
            "WECOM_ENCODING_AES_KEY", "WECOM_OPEN_KFID",
        )
        values = [os.getenv(name, "").strip() for name in names]
        if not any(values):
            return None
        missing = [name for name, value in zip(names, values) if not value]
        if missing:
            raise ValueError(f"Missing WeCom settings: {', '.join(missing)}")
        return cls(*values, Path(os.getenv("WECOM_DB_PATH", "data/wecom.sqlite3")))
