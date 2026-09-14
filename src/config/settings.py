"""应用配置模块。

使用 ``pydantic-settings`` 统一管理配置，加载优先级为：

    环境变量  >  项目根目录 ``.env`` 文件  >  本模块中的默认值

配置项按用途分组：应用基础、MySQL、Redis、JWT、CORS、百度人脸识别、业务参数。
其它模块统一通过以下方式读取配置::

    from src.config.settings import settings

    print(settings.APP_NAME)
    print(settings.database_url)
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from urllib.parse import quote_plus

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: 项目根目录（src/config/settings.py -> src/config -> src -> 项目根）
BASE_DIR: Path = Path(__file__).resolve().parents[2]

#: .env 配置文件路径
ENV_FILE: Path = BASE_DIR / ".env"

#: 默认 JWT 密钥，生产环境必须通过 .env 覆盖
DEFAULT_JWT_SECRET_KEY: str = "change-me-in-production-please"

#: 合法的日志级别
_LOG_LEVELS: frozenset[str] = frozenset(
    {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"}
)

#: 合法的人脸识别提供方
_FACE_PROVIDERS: frozenset[str] = frozenset({"baidu", "local"})

#: 合法的 LLM 提供方
_LLM_PROVIDERS: frozenset[str] = frozenset({"mock", "openai_compat"})


class Settings(BaseSettings):
    """项目全局配置。"""

    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ------------------------------------------------------------------
    # 应用基础配置
    # ------------------------------------------------------------------
    APP_NAME: str = "乡村基层活动智能签到管理平台"
    APP_DESCRIPTION: str = (
        "面向乡村基层的活动签到管理系统：以家庭为单位进行人脸签到，"
        "管理员可创建签到活动、审批请假并查看统计报表。"
    )
    APP_VERSION: str = "0.1.0"
    ENV: str = "development"
    DEBUG: bool = True
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    API_PREFIX: str = "/api"
    LOG_LEVEL: str = "INFO"

    # ------------------------------------------------------------------
    # MySQL 数据库配置
    # ------------------------------------------------------------------
    DB_HOST: str = "127.0.0.1"
    DB_PORT: int = 3306
    DB_USER: str = "root"
    DB_PASSWORD: str = ""
    DB_NAME: str = "attendance"
    DB_CHARSET: str = "utf8mb4"
    DB_ECHO: bool = False
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20
    DB_POOL_RECYCLE: int = 3600
    #: 完整数据库连接串，配置后将优先生效（忽略上面的分项配置）
    DATABASE_URL: str | None = None

    # ------------------------------------------------------------------
    # Redis 配置
    # ------------------------------------------------------------------
    REDIS_HOST: str = "127.0.0.1"
    REDIS_PORT: int = 6379
    REDIS_DB: int = 0
    REDIS_PASSWORD: str = ""
    REDIS_KEY_PREFIX: str = "attendance:"
    #: 完整 Redis 连接串，配置后将优先生效
    REDIS_URL: str | None = None

    # ------------------------------------------------------------------
    # JWT 认证配置
    # ------------------------------------------------------------------
    JWT_SECRET_KEY: str = DEFAULT_JWT_SECRET_KEY
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 120
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # ------------------------------------------------------------------
    # CORS 跨域配置（多个来源用英文逗号分隔）
    # ------------------------------------------------------------------
    CORS_ORIGINS: str = "http://localhost:5173,http://127.0.0.1:5173"
    CORS_ALLOW_CREDENTIALS: bool = True

    # ------------------------------------------------------------------
    # 百度 AI 人脸识别配置（阶段四接入）
    # ------------------------------------------------------------------
    BAIDU_FACE_API_KEY: str = ""
    BAIDU_FACE_SECRET_KEY: str = ""
    BAIDU_FACE_GROUP_ID: str = "attendance_family"
    #: 人脸比对通过阈值（0-100，分数越高越严格）
    FACE_MATCH_THRESHOLD: float = 80.0
    #: 人脸识别提供方：``baidu`` 使用百度 AI 人脸库；``local`` 为本地模式
    #: （不调用外部服务、不做人脸比对，仅用于前端联调与流程自测，生产环境禁止使用）
    FACE_PROVIDER: str = "baidu"
    #: 人脸照片存储目录（相对项目根目录）
    FACE_UPLOAD_DIR: str = "uploads/face"
    #: 单张人脸照片大小上限（MB）
    FACE_MAX_IMAGE_MB: int = 2
    #: 搜索时最多返回的候选数量
    FACE_SEARCH_MAX_CANDIDATES: int = 5
    #: 录入人脸前是否先做一次人脸检测（拒绝无人脸 / 多张人脸的照片）
    FACE_DETECT_ON_REGISTER: bool = True
    #: 百度 access_token 提前刷新余量（秒）
    BAIDU_FACE_TOKEN_REFRESH_MARGIN: int = 3600

    # ------------------------------------------------------------------
    # AI 智能助手配置（阶段七：LLM / Agent / AIGC）
    # ------------------------------------------------------------------
    #: LLM 提供方：``mock`` 无密钥即可跑通全流程（默认）；
    #: ``openai_compat`` 调用任意 OpenAI 兼容接口（DeepSeek / 豆包 / 通义等）
    LLM_PROVIDER: str = "mock"
    #: OpenAI 兼容服务地址，如 https://api.deepseek.com/v1（不要带 /chat/completions）
    LLM_BASE_URL: str = ""
    #: 服务商 API Key（Bearer 认证）
    LLM_API_KEY: str = ""
    #: 模型名称，如 deepseek-chat / doubao-pro-32k
    LLM_MODEL: str = ""
    #: 单次 LLM 请求超时（秒）
    LLM_TIMEOUT_SECONDS: float = 30.0
    #: 单轮对话内最多允许的工具调用轮数（超过则强制停止并告知用户）
    AI_TOOL_MAX_ROUNDS: int = 4
    #: 轻量 RAG：注入系统提示词的知识库 Q&A 条数
    AI_KNOWLEDGE_TOP_K: int = 3

    # ------------------------------------------------------------------
    # 业务参数
    # ------------------------------------------------------------------
    #: 创建活动时未显式指定迟到阈值所使用的默认值（分钟）
    DEFAULT_LATE_THRESHOLD_MINUTES: int = 15

    # ------------------------------------------------------------------
    # 校验器
    # ------------------------------------------------------------------
    @field_validator("LOG_LEVEL")
    @classmethod
    def _validate_log_level(cls, value: str) -> str:
        """校验并规范化日志级别。"""
        level = value.strip().upper()
        if level not in _LOG_LEVELS:
            raise ValueError(
                f"无效的日志级别：{value}，可选值：{', '.join(sorted(_LOG_LEVELS))}"
            )
        return level

    @field_validator("CORS_ORIGINS")
    @classmethod
    def _normalize_cors_origins(cls, value: str) -> str:
        """去除多余空格与空项，统一为逗号分隔字符串。"""
        return ",".join(item.strip() for item in value.split(",") if item.strip())

    @field_validator("FACE_PROVIDER")
    @classmethod
    def _validate_face_provider(cls, value: str) -> str:
        """校验人脸识别提供方。"""
        provider = value.strip().lower()
        if provider not in _FACE_PROVIDERS:
            raise ValueError(
                f"无效的人脸识别提供方：{value}，可选值：{', '.join(sorted(_FACE_PROVIDERS))}"
            )
        return provider

    @field_validator("LLM_PROVIDER")
    @classmethod
    def _validate_llm_provider(cls, value: str) -> str:
        """校验 LLM 提供方。"""
        provider = value.strip().lower()
        if provider not in _LLM_PROVIDERS:
            raise ValueError(
                f"无效的 LLM 提供方：{value}，可选值：{', '.join(sorted(_LLM_PROVIDERS))}"
            )
        return provider

    # ------------------------------------------------------------------
    # 派生配置
    # ------------------------------------------------------------------
    @property
    def database_url(self) -> str:
        """SQLAlchemy 使用的数据库连接串。

        若显式配置了 ``DATABASE_URL`` 则直接使用，否则由 MySQL 分项配置拼接，
        密码中的特殊字符会被自动转义。
        """
        if self.DATABASE_URL:
            return self.DATABASE_URL

        password = quote_plus(self.DB_PASSWORD) if self.DB_PASSWORD else ""
        credentials = f"{self.DB_USER}:{password}" if password else self.DB_USER
        return (
            f"mysql+pymysql://{credentials}"
            f"@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"
            f"?charset={self.DB_CHARSET}"
        )

    @property
    def redis_url(self) -> str:
        """Redis 连接串。

        若显式配置了 ``REDIS_URL`` 则直接使用，否则由 Redis 分项配置拼接。
        """
        if self.REDIS_URL:
            return self.REDIS_URL

        credentials = (
            f":{quote_plus(self.REDIS_PASSWORD)}@" if self.REDIS_PASSWORD else ""
        )
        return (
            f"redis://{credentials}{self.REDIS_HOST}:{self.REDIS_PORT}/{self.REDIS_DB}"
        )

    @property
    def cors_origins(self) -> list[str]:
        """CORS 允许的来源列表。"""
        if self.CORS_ORIGINS.strip() == "*":
            return ["*"]
        return [item for item in self.CORS_ORIGINS.split(",") if item]

    @property
    def is_production(self) -> bool:
        """是否为生产环境。"""
        return self.ENV.strip().lower() in {"production", "prod"}

    @property
    def face_upload_path(self) -> Path:
        """人脸照片存储目录的绝对路径。"""
        path = Path(self.FACE_UPLOAD_DIR)
        return path if path.is_absolute() else BASE_DIR / path

    @property
    def face_max_image_bytes(self) -> int:
        """单张人脸照片允许的最大字节数。"""
        return self.FACE_MAX_IMAGE_MB * 1024 * 1024

    @property
    def baidu_face_configured(self) -> bool:
        """是否已配置百度人脸识别密钥。"""
        return bool(self.BAIDU_FACE_API_KEY.strip() and self.BAIDU_FACE_SECRET_KEY.strip())

    @property
    def using_default_jwt_secret(self) -> bool:
        """是否仍在使用默认 JWT 密钥（生产环境需覆盖）。"""
        return self.JWT_SECRET_KEY == DEFAULT_JWT_SECRET_KEY

    @property
    def llm_configured(self) -> bool:
        """是否已配置完整的 OpenAI 兼容接口信息（地址 + 密钥 + 模型）。"""
        return bool(
            self.LLM_BASE_URL.strip()
            and self.LLM_API_KEY.strip()
            and self.LLM_MODEL.strip()
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """获取全局配置单例（带缓存，重复调用不会重复解析 .env）。"""
    return Settings()


#: 全局配置实例，业务代码直接导入使用
settings: Settings = get_settings()
