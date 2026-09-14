"""乡村基层活动智能签到管理平台 —— 应用入口。

职责：

1. 创建 FastAPI 应用实例，配置标题、描述、版本等元信息；
2. 注册 CORS 跨域中间件；
3. 注册全局异常处理器，保证所有响应均为统一格式
   ``{"code": 0, "message": "success", "data": {}}``；
4. 提供 ``GET /health`` 健康检查接口；
5. 预留各业务模块的路由注册位置（按开发计划逐步放开）。

启动方式::

    # 方式一：直接运行（端口、调试开关读取 .env 配置）
    python main.py

    # 方式二：使用 uvicorn 命令
    uvicorn main:app --host 0.0.0.0 --port 8000 --reload

接口文档：http://127.0.0.1:8000/docs
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from src.auth.router import router as auth_router
from src.assistant.router import router as assistant_router
from src.checkin.router import router as checkin_router
from src.common.exceptions import BusinessError
from src.common.response import ResponseCode, error_response, success_response
from src.config.settings import settings
from src.event.router import router as event_router
from src.family.router import router as family_router
from src.face.router import router as face_router
from src.leave.router import router as leave_router
from src.log.context import ClientIpMiddleware
from src.log.router import router as log_router
from src.member.router import router as member_router
from src.statistics.router import router as statistics_router
from src.user.router import router as user_router

logger = logging.getLogger(__name__)

#: Starlette 默认英文提示 -> 中文提示（自定义 detail 时保持原样）
_DEFAULT_HTTP_ERROR_MESSAGES: dict[str, str] = {
    "Not Found": "请求的接口或资源不存在",
    "Method Not Allowed": "请求方法不被允许",
    "Unauthorized": "未认证或认证已失效",
    "Forbidden": "没有访问权限",
    "Internal Server Error": "服务器内部错误",
}

#: pydantic 常见校验错误类型 -> 中文提示
_VALIDATION_ERROR_MESSAGES: dict[str, str] = {
    "missing": "该字段为必填项",
    "extra_forbidden": "不支持的字段",
    "string_too_short": "长度不足",
    "string_too_long": "长度超出限制",
    "string_pattern_mismatch": "格式不正确",
    "string_type": "必须为字符串",
    "int_parsing": "必须为整数",
    "int_type": "必须为整数",
    "float_parsing": "必须为数字",
    "bool_parsing": "必须为布尔值",
    "enum": "取值不在允许范围内",
    "value_error": "取值不合法",
    "json_invalid": "请求体不是合法的 JSON",
}


def format_validation_errors(errors: list[dict[str, Any]]) -> str:
    """把 pydantic 校验错误整理成中文可读提示。

    形如：``username: 长度不足（最少 4 个字符）；password: 该字段为必填项``
    """
    details: list[str] = []
    for error in errors:
        location = ".".join(
            str(part) for part in error.get("loc", ()) if part != "body"
        ) or "请求体"
        raw_message = str(error.get("msg", "参数不合法"))
        error_type = str(error.get("type", ""))
        context = error.get("ctx") or {}

        if error_type == "value_error" and "Value error, " in raw_message:
            # 自定义校验器的中文提示，去掉 pydantic 前缀
            message = raw_message.split("Value error, ", 1)[1]
        else:
            message = _VALIDATION_ERROR_MESSAGES.get(error_type, raw_message)

        if error_type == "string_too_short" and "min_length" in context:
            message = f"{message}（最少 {context['min_length']} 个字符）"
        elif error_type == "string_too_long" and "max_length" in context:
            message = f"{message}（最多 {context['max_length']} 个字符）"
        elif error_type == "string_pattern_mismatch":
            message = f"{message}（应为：{context.get('pattern', '')}）"
        elif error_type == "enum":
            message = f"{message}（可选值：{'/'.join(str(v) for v in context.get('expected', ''))}）"

        details.append(f"{location}: {message}")
    return "；".join(details)


def configure_logging() -> None:
    """按配置初始化日志（级别由 .env 中的 LOG_LEVEL 控制）。"""
    logging.basicConfig(
        level=getattr(logging, settings.LOG_LEVEL, logging.INFO),
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """应用生命周期管理：启动时打印运行信息，关闭时释放资源。"""
    configure_logging()
    logger.info("=" * 64)
    logger.info("%s v%s 启动中 ...", settings.APP_NAME, settings.APP_VERSION)
    logger.info("运行环境：%s | 调试模式：%s", settings.ENV, settings.DEBUG)
    logger.info("接口文档：http://127.0.0.1:%s/docs", settings.PORT)
    logger.info("接口前缀：%s | 数据库：%s", settings.API_PREFIX, settings.DB_NAME)
    logger.info("已注册 %d 个接口路径（完整列表见 /openapi.json）", len(app.openapi()["paths"]))
    if settings.using_default_jwt_secret:
        logger.warning("当前使用默认 JWT_SECRET_KEY，请在生产环境通过 .env 覆盖！")
    if settings.FACE_PROVIDER == "local":
        logger.warning(
            "人脸识别运行在本地模式（FACE_PROVIDER=local）：仅保存照片与录入状态，"
            "不做人脸比对，请勿用于生产环境！"
        )
    elif not settings.baidu_face_configured:
        logger.warning(
            "未配置百度人脸识别密钥（BAIDU_FACE_API_KEY / BAIDU_FACE_SECRET_KEY），"
            "人脸录入与识别接口将返回 503；如仅需前端联调可设置 FACE_PROVIDER=local"
        )
    if settings.LLM_PROVIDER == "mock":
        logger.warning(
            "AI 智能助手运行在 mock 模式（LLM_PROVIDER=mock）：回答为模板化提示，"
            "但工具调用与真实数据查询链路完全可用；"
            "配置 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL 后可获得真实智能回答"
        )
    elif not settings.llm_configured:
        logger.warning(
            "已设置 LLM_PROVIDER=%s 但未配置 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL，"
            "AI 对话接口将返回 503",
            settings.LLM_PROVIDER,
        )
    logger.info("=" * 64)

    yield

    logger.info("%s 已关闭", settings.APP_NAME)


app = FastAPI(
    title=settings.APP_NAME,
    description=settings.APP_DESCRIPTION,
    version=settings.APP_VERSION,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    lifespan=lifespan,
)

# ----------------------------------------------------------------------
# 跨域配置：允许前端管理后台（Vue 3）与微信小程序调试端访问
# ----------------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=settings.CORS_ALLOW_CREDENTIALS,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)

# 请求来源 IP 注入（供操作日志埋点使用）；纯 ASGI 中间件，不包装响应、不影响流式输出
app.add_middleware(ClientIpMiddleware)


# ----------------------------------------------------------------------
# 全局异常处理：保证异常场景下响应体仍是统一格式
# ----------------------------------------------------------------------
@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    """处理 HTTP 协议层异常（404 / 405 等），保留原始 HTTP 状态码。"""
    logger.warning(
        "HTTP 异常 | %s %s | status=%s | detail=%s",
        request.method,
        request.url.path,
        exc.status_code,
        exc.detail,
    )
    # 框架抛出的英文默认提示统一本地化，业务自定义 detail 原样返回
    message = _DEFAULT_HTTP_ERROR_MESSAGES.get(str(exc.detail), str(exc.detail))
    return JSONResponse(
        status_code=exc.status_code,
        content=error_response(message=message, code=exc.status_code),
    )


@app.exception_handler(BusinessError)
async def business_error_handler(
    request: Request, exc: BusinessError
) -> JSONResponse:
    """处理业务异常，返回统一格式。

    HTTP 状态码默认与业务码一致（如 401 / 403 / 404 / 409），
    前端拦截器可继续按状态码处理（例如 401 跳转登录页）。
    """
    logger.warning(
        "业务异常 | %s %s | code=%s | %s",
        request.method,
        request.url.path,
        exc.code,
        exc.message,
    )
    return JSONResponse(
        status_code=exc.http_status,
        content=error_response(
            message=exc.message, code=exc.code, data=exc.data
        ),
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """处理请求参数校验失败，返回业务错误码 400（HTTP 200）。"""
    details = format_validation_errors(exc.errors())
    logger.warning(
        "参数校验失败 | %s %s | %s", request.method, request.url.path, details
    )
    return JSONResponse(
        status_code=200,
        content=error_response(
            message=f"参数校验失败：{details}",
            code=ResponseCode.PARAM_ERROR,
            # errors() 中可能包含异常对象等不可直接 JSON 序列化的内容，需先编码
            data={"errors": jsonable_encoder(exc.errors())},
        ),
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """处理未捕获异常，避免向客户端泄露堆栈信息。"""
    logger.exception("服务器内部错误 | %s %s", request.method, request.url.path)
    message = str(exc) if settings.DEBUG else "服务器内部错误，请稍后重试"
    return JSONResponse(
        status_code=500,
        content=error_response(message=message, code=ResponseCode.SERVER_ERROR),
    )


# ----------------------------------------------------------------------
# 系统接口
# ----------------------------------------------------------------------
#: 演示前端静态资源目录（web/，与后端同源托管，无需跨域配置）
WEB_DIR = Path(__file__).resolve().parent / "web"


@app.get("/", tags=["系统"], summary="演示前端页面")
async def root() -> FileResponse:
    """根路径，返回演示前端页面（登录 / 活动签到 / 请假 / AI 智能助手）。"""
    return FileResponse(WEB_DIR / "index.html", media_type="text/html")


app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


@app.get("/health", tags=["系统"], summary="健康检查")
async def health_check() -> dict[str, Any]:
    """健康检查接口。

    :return: ``{"code": 0, "message": "success", "data": {"status": "ok"}}``
    """
    return success_response(data={"status": "ok"})


# ======================================================================
# 业务模块路由注册（按开发计划逐步放开）
#
# 说明：
#   1. 各模块需在自身目录下提供 router.py 并导出名为 router 的 APIRouter 实例；
#   2. 所有业务接口统一挂载到 settings.API_PREFIX（默认 /api）之下；
#   3. 模块开发完成并自测通过后，再取消对应行注释。
# ======================================================================

# --- 阶段二：认证与用户（已完成） ---
app.include_router(auth_router, prefix=f"{settings.API_PREFIX}/auth", tags=["认证"])
app.include_router(user_router, prefix=f"{settings.API_PREFIX}/users", tags=["用户管理"])

# --- 阶段三：家庭与成员（已完成） ---
app.include_router(family_router, prefix=f"{settings.API_PREFIX}/families", tags=["家庭管理"])
app.include_router(member_router, prefix=f"{settings.API_PREFIX}/members", tags=["家庭成员"])

# --- 阶段四：人脸识别（已完成） ---
app.include_router(face_router, prefix=f"{settings.API_PREFIX}/face", tags=["人脸识别"])

# --- 阶段五：活动与签到（已完成） ---
app.include_router(event_router, prefix=f"{settings.API_PREFIX}/events", tags=["签到活动"])
app.include_router(checkin_router, prefix=f"{settings.API_PREFIX}/checkins", tags=["签到业务"])
app.include_router(leave_router, prefix=f"{settings.API_PREFIX}/leaves", tags=["请假管理"])

# --- 阶段六：统计与日志（已完成） ---
app.include_router(
    statistics_router, prefix=f"{settings.API_PREFIX}/statistics", tags=["统计报表"]
)
app.include_router(log_router, prefix=f"{settings.API_PREFIX}/logs", tags=["操作日志"])

# --- 阶段七：AI 智能助手（已完成） ---
app.include_router(
    assistant_router,
    prefix=f"{settings.API_PREFIX}/assistant",
    tags=["AI 智能助手"],
)


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=settings.DEBUG,
        log_level=settings.LOG_LEVEL.lower(),
    )
