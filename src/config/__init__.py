"""配置模块：集中管理数据库、Redis、JWT 密钥等运行配置。

配置项通过 ``pydantic-settings`` 从环境变量与项目根目录的 ``.env`` 文件加载，
业务代码统一使用::

    from src.config.settings import settings
"""

from src.config.settings import Settings, get_settings, settings

__all__ = ["Settings", "get_settings", "settings"]
