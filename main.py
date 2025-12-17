import logging
from io import BytesIO
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from PIL import Image
import uvicorn

from core import (
    DEFAULT_P_ID,
    compute_sign,
    fetch_benefit_status_safe,
    fetch_remove_status_safe,
    fetch_wm_status_safe,
    generate_e_id,
    send_trial_request_safe,
    upload_remove_wm_safe,
)
from database import (
    init_database,
    insert_api_call,
    update_result_url,
)

# 常量定义
LOG_DIR = Path("logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "app.log"

MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/jpg", "image/webp"}

# 配置日志
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
logging.getLogger("httpx").setLevel(logging.INFO)
logging.getLogger("httpcore").setLevel(logging.INFO)
logging.getLogger("aiosqlite").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


def _get_client_ip(request: Request) -> str:
    """获取客户端真实IP地址"""
    # 优先从X-Forwarded-For获取
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        # X-Forwarded-For可能包含多个IP,取第一个
        return forwarded_for.split(",")[0].strip()

    # 其次从X-Real-IP获取
    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        return real_ip.strip()

    # 最后使用直连IP
    if request.client:
        return request.client.host

    return "unknown"


def _get_user_agent(request: Request) -> str:
    """获取User-Agent"""
    return request.headers.get("User-Agent", "unknown")


def _parse_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _benefits_map(benefit_status: dict) -> dict[str, dict]:
    subscriptions = benefit_status.get("subscriptions") or []
    if not subscriptions:
        return {}
    benefits = subscriptions[0].get("benefits") or []
    return {item.get("key"): item for item in benefits if item.get("key")}


def _validate_against_benefits(request: "ImageEraseRequest", benefit_status: dict) -> None:
    benefits = _benefits_map(benefit_status)

    # 检查上传大小
    in_size_limit = _parse_int((benefits.get("in_size") or {}).get("limit"))
    if in_size_limit and in_size_limit > 0 and request.file_size_bytes:
        if request.file_size_bytes > in_size_limit:
            limit_mb = in_size_limit / 1024 / 1024
            actual_mb = request.file_size_bytes / 1024 / 1024
            raise HTTPException(
                status_code=400,
                detail=(
                    f"上传图片大小超过限制: {actual_mb:.2f}MB，"
                    f"仅支持 {limit_mb:.2f}MB 及以下"
                ),
            )

    # 检查输入分辨率最大边
    if request.width and request.height:
        max_edge = max(request.width, request.height)
        in_edge_threshold = _parse_int((benefits.get("in_edge") or {}).get("threshold"))
        if in_edge_threshold and in_edge_threshold > 0 and max_edge > in_edge_threshold:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"上传图片最大边超过限制: {max_edge}px，"
                    f"仅支持 {in_edge_threshold}px 及以下"
                ),
            )


async def _extract_image_info(img: UploadFile) -> tuple[bytes, int, int | None, int | None]:
    """提取图片信息：字节数据、大小、宽度、高度"""
    data = await img.read()
    size = len(data)
    width = height = None
    try:
        with Image.open(BytesIO(data)) as im:
            width, height = im.size
            logger.debug(f"Image info: format={im.format}, size={width}x{height}, mode={im.mode}")
    except Exception as e:
        logger.warning(f"Failed to extract image dimensions: {e}")
        width = height = None
    return data, size, width, height


def _validate_upload_file(file: UploadFile, max_size: int = MAX_FILE_SIZE) -> None:
    """验证上传文件的类型和大小"""
    if not file.content_type or file.content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件类型: {file.content_type}。仅支持: {', '.join(ALLOWED_IMAGE_TYPES)}"
        )

    # 注意：FastAPI 的 UploadFile 需要读取后才能获取大小，这里只做类型检查
    # 大小检查在读取后进行

# 创建 FastAPI 应用实例
app = FastAPI(
    title="Apeaksoft Watermark Remover API",
    description="Apeaksoft Watermark Remover 接口逆向服务",
    version="1.0.0",
    docs_url="/swagger",
)

# 启用 CORS，允许本地前端和部署域访问
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup_event():
    """应用启动时初始化数据库"""
    await init_database()
    logger.info("应用启动完成")


class ImageEraseRequest(BaseModel):
    image_path: str = Field(..., description="图片路径或名称")
    watermark_region: dict | None = Field(None, description="擦除区域坐标")
    file_size_bytes: int | None = Field(None, description="文件大小（字节）")
    width: int | None = Field(None, description="图片宽度")
    height: int | None = Field(None, description="图片高度")


class ImageEraseResponse(BaseModel):
    token: str = Field(..., description="处理任务Token")
    message: str = Field(..., description="响应消息")


class EraseStatusRequest(BaseModel):
    token: str = Field(..., description="擦除任务Token")

# API 路由
@app.post("/api/erase", response_model=ImageEraseResponse, tags=["Erase"])
async def erase_image(
    request: Request,
    img: UploadFile = File(..., description="原图文件"),
    mask: UploadFile = File(..., description="掩膜文件"),
):
    """
    擦除图片指定区域 (multipart/form-data)

    - **img**: 必填,原图文件
    - **mask**: 必填,掩膜文件
    """
    request_id = generate_e_id()  # 用于追踪整个请求
    client_ip = _get_client_ip(request)
    user_agent = _get_user_agent(request)
    logger.info(f"[{request_id}] 开始处理图片擦除请求 - IP: {client_ip}")

    try:
        # 验证文件类型
        _validate_upload_file(img)
        _validate_upload_file(mask)

        # 第一步: 发送 trial 请求
        logger.info(f"[{request_id}] 发送 trial 请求")
        await send_trial_request_safe(DEFAULT_P_ID)

        # 生成本次流程的 e_id
        current_eid = generate_e_id()
        logger.debug(f"[{request_id}] 生成 e_id: {current_eid}")

        # 解析上传文件信息
        img_bytes, file_size_bytes, width, height = await _extract_image_info(img)

        # 验证文件大小
        if file_size_bytes > MAX_FILE_SIZE:
            raise HTTPException(
                status_code=413,
                detail=f"文件过大: {file_size_bytes / 1024 / 1024:.2f}MB，最大允许 {MAX_FILE_SIZE / 1024 / 1024:.0f}MB"
            )

        mask_bytes = await mask.read()
        image_path = img.filename or "uploaded_image"
        effective_sign, timestamp = compute_sign(img_bytes)

        logger.info(
            f"[{request_id}] 文件信息: path={image_path}, size={file_size_bytes}, "
            f"dimensions={width}x{height}, mask_size={len(mask_bytes)}, "
            f"sign_generated=True, e_id={current_eid}, timestamp={timestamp}"
        )

        # 构造上下文并校验权益限制
        req_ctx = ImageEraseRequest(
            image_path=image_path,
            watermark_region=None,
            file_size_bytes=file_size_bytes,
            width=width,
            height=height,
        )

        logger.info(f"[{request_id}] 获取权益状态")
        benefit_status = await fetch_benefit_status_safe()
        if benefit_status:
            logger.debug(f"[{request_id}] 验证权益限制")
            _validate_against_benefits(req_ctx, benefit_status)
        else:
            logger.warning(f"[{request_id}] 权益状态不可用，跳过配额验证")

        # 转发到 removeWM/upload
        logger.info(f"[{request_id}] 上传文件到远程服务")
        upstream_resp = await upload_remove_wm_safe(
            img_bytes=img_bytes,
            img_filename=img.filename,
            img_content_type=img.content_type,
            mask_bytes=mask_bytes,
            mask_filename=mask.filename,
            mask_content_type=mask.content_type,
            sign=effective_sign,
            name=image_path,
            e_id=current_eid,
        )

        if not upstream_resp:
            raise HTTPException(
                status_code=502,
                detail="远程服务上传失败"
            )

        # 检查 upstream_resp 状态
        upstream_status = upstream_resp.get("status")
        if upstream_status != "200":
            logger.error(f"[{request_id}] 上传失败: status={upstream_status}, resp={upstream_resp}")
            raise HTTPException(
                status_code=502,
                detail=f"远程服务返回错误: {upstream_resp.get('message', '未知错误')}"
            )

        logger.info(f"[{request_id}] 上传成功: {upstream_resp}")

        # 提取 token
        token = (
            upstream_resp.get("token")
            or (upstream_resp.get("data") or {}).get("token")
            or (upstream_resp.get("result") or {}).get("token")
        )

        if not token:
            logger.error(f"[{request_id}] 无法从上传响应中提取 token. resp={upstream_resp}")
            raise HTTPException(
                status_code=502,
                detail="无法获取处理任务Token"
            )

        logger.info(f"[{request_id}] 查询 WM 处理状态")
        wm_resp = await fetch_wm_status_safe(token=token, e_id=current_eid)

        if not wm_resp:
            logger.error(f"[{request_id}] WM 状态查询失败")
            raise HTTPException(
                status_code=502,
                detail="水印处理状态查询失败"
            )

        # 检查 wm_resp 状态
        wm_status = wm_resp.get("status")
        if wm_status != "200":
            logger.error(f"[{request_id}] WM 处理失败: status={wm_status}, resp={wm_resp}")
            raise HTTPException(
                status_code=502,
                detail=f"图片擦除失败: {wm_resp.get('message', '未知错误')}"
            )

        logger.info(f"[{request_id}] WM 状态响应成功: {wm_resp}")

        # 提取结果URL
        result_url = None
        if wm_resp.get("status") == "200":
            # 尝试从多个可能的字段提取URL
            result_url = (
                wm_resp.get("url")
                or (wm_resp.get("data") or {}).get("url")
                or (wm_resp.get("result") or {}).get("url")
            )

        # 将API调用记录存入数据库
        try:
            await insert_api_call(
                ip_address=client_ip,
                user_agent=user_agent,
                image_filename=image_path,
                image_data=img_bytes,
                image_content_type=img.content_type,
                image_size_bytes=file_size_bytes,
                image_width=width,
                image_height=height,
                token=token,
                e_id=current_eid,
                result_url=result_url,
            )
            logger.info(f"[{request_id}] API调用记录已存入数据库")
        except Exception as db_err:
            logger.error(f"[{request_id}] 数据库记录失败: {db_err}", exc_info=True)
            # 数据库错误不影响主流程,继续执行

        logger.info(f"[{request_id}] 图片擦除请求处理完成")

        # 返回简化的响应
        return ImageEraseResponse(
            token=token,
            message="已提交擦除请求，正在处理中..",
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"[{request_id}] 图片擦除失败")
        raise HTTPException(
            status_code=500,
            detail=f"内部服务错误: {str(e)}"
        )


@app.post("/api/erase/status", tags=["Erase"])
async def get_erase_status(
    request: Request,
    payload: EraseStatusRequest,
):
    """
    查询擦除任务状态

    - **token**: 必填,擦除任务Token
    """
    request_id = generate_e_id()
    client_ip = _get_client_ip(request)
    logger.info(f"[{request_id}] 查询擦除状态 - IP: {client_ip}, token={payload.token}")

    upstream_resp = await fetch_remove_status_safe(token=payload.token)
    print(upstream_resp)
    if upstream_resp is None:
        logger.error(f"[{request_id}] 远程服务查询失败")
        raise HTTPException(
            status_code=502,
            detail="远程服务查询失败"
        )

    result_url = (
        upstream_resp.get("url")
        or (upstream_resp.get("data") or {}).get("url")
        or (upstream_resp.get("result") or {}).get("url")
    )

    if result_url:
        try:
            await update_result_url(payload.token, result_url)
        except Exception as db_err:
            logger.warning(f"[{request_id}] 更新结果URL失败: {db_err}", exc_info=True)

    logger.info(f"[{request_id}] 状态查询完成: {upstream_resp}")
    return JSONResponse(content=upstream_resp)


# 主程序入口
if __name__ == '__main__':
    print("🚀 启动 Apeaksoft Watermark Remover API 服务...")
    print("📖 Swagger 文档地址: http://127.0.0.1:8000/swagger")

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        log_level="debug"
    )
