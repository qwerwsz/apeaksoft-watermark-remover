import base64
import hashlib
import logging
import re
import secrets
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator

import httpx
import mmh3
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from fake_useragent import UserAgent

# 接口端点
TRIAL_ENDPOINT = "https://account.api.apeaksoft.com/v9/product/trial"
BENEFIT_STATUS_ENDPOINT = "https://account.api.apeaksoft.com/v9/benefit/status"
REMOVE_WM_UPLOAD_ENDPOINT = "https://ai-api.apeaksoft.com/v6/removeWM/upload"
REMOVE_WM_STATUS_ENDPOINT = "https://ai-api.apeaksoft.com/v6/removeWM/WM"
REMOVE_WM_STATUS_POLL_ENDPOINT = "https://ai-api.apeaksoft.com/v6/removeWM/status"

# 默认参数
DEFAULT_P_ID = "56"
DEFAULT_E_ID: str | None = None

# 加密密钥
SIGN_KEY = b"5FA2MKT7miJ/sGTb"
SIGN_IV = b"Aryx2NC77xtTX8Ju"

# User-Agent 配置
FALLBACK_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/143.0.0.0 Safari/537.36 Edg/143.0.0.0"
)

# 通用请求头
COMMON_HEADERS = {
    "accept": "*/*",
    "accept-language": "zh-CN,zh;q=0.9",
    "origin": "https://www.apeaksoft.com",
    "priority": "u=1, i",
    "referer": "https://www.apeaksoft.com/",
}

# 网络请求配置
DEFAULT_TIMEOUT = 10.0
UPLOAD_TIMEOUT = 30.0
MAX_RETRIES = 3

logger = logging.getLogger(__name__)
ua_provider = UserAgent(
    browsers=["edge", "chrome"],
    fallback=FALLBACK_USER_AGENT,
)

# 全局 HTTP 客户端（使用连接池）
_http_client: httpx.AsyncClient | None = None


async def get_http_client() -> httpx.AsyncClient:
    """获取共享的 HTTP 客户端实例（带连接池）"""
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            timeout=DEFAULT_TIMEOUT,
            limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
            follow_redirects=True,
        )
    return _http_client


async def close_http_client() -> None:
    """关闭 HTTP 客户端"""
    global _http_client
    if _http_client is not None and not _http_client.is_closed:
        await _http_client.aclose()
        _http_client = None


def _get_user_agent() -> str:
    try:
        return ua_provider.random
    except Exception as exc:
        logger.debug("fake-useragent failed, using fallback UA: %s", exc)
        return FALLBACK_USER_AGENT


def _extract_major_version(ua: str) -> str:
    match = re.search(r"(?:Edg|Chrome|Chromium)/(\d+)", ua)
    if match:
        return match.group(1)
    return "99"


def _build_sec_ch_ua(ua: str) -> str:
    major = _extract_major_version(ua)
    ua_lower = ua.lower()
    if "edg" in ua_lower:
        return f'"Microsoft Edge";v="{major}", "Chromium";v="{major}", "Not A(Brand";v="99"'
    if "chrome" in ua_lower or "chromium" in ua_lower:
        return f'"Google Chrome";v="{major}", "Chromium";v="{major}", "Not A(Brand";v="99"'
    return f'"Chromium";v="{major}", "Not A(Brand";v="99"'


def _detect_mobile_flag(ua: str) -> str:
    return "?1" if "mobile" in ua.lower() else "?0"


def _detect_platform_token(ua: str) -> str:
    ua_lower = ua.lower()
    if "windows" in ua_lower:
        return '"Windows"'
    if "mac os x" in ua_lower or "macintosh" in ua_lower:
        return '"macOS"'
    if "android" in ua_lower:
        return '"Android"'
    if "linux" in ua_lower:
        return '"Linux"'
    return '"Unknown"'


def _build_client_hints(ua: str) -> dict[str, str]:
    return {
        "user-agent": ua,
        "sec-ch-ua": _build_sec_ch_ua(ua),
        "sec-ch-ua-mobile": _detect_mobile_flag(ua),
        "sec-ch-ua-platform": _detect_platform_token(ua),
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
    }


def _build_headers(content_type: str | None = "application/x-www-form-urlencoded; charset=UTF-8") -> dict[str, str]:
    headers = COMMON_HEADERS.copy()
    ua = _get_user_agent()
    headers.update(_build_client_hints(ua))
    if content_type:
        headers["content-type"] = content_type
    else:
        headers.pop("content-type", None)
    return headers


async def send_trial_request(p_id: str = DEFAULT_P_ID) -> dict:
    """发送 trial 请求"""
    headers = _build_headers()
    logger.debug(f"Trial request headers: {headers}")

    client = await get_http_client()
    response = await client.post(
        TRIAL_ENDPOINT,
        data={"p_id": p_id},
        headers=headers,
    )
    response.raise_for_status()
    return response.json()


async def send_trial_request_safe(p_id: str = DEFAULT_P_ID) -> dict | None:
    """发送 trial 请求（安全版本，捕获异常）"""
    try:
        result = await send_trial_request(p_id)
        logger.debug(f"Trial request sent for p_id={p_id}, result={result}")
        return result
    except httpx.HTTPStatusError as exc:
        logger.warning(f"Trial request HTTP error for p_id={p_id}: {exc.response.status_code}")
        return None
    except Exception as exc:
        logger.warning(f"Trial request failed for p_id={p_id}: {exc}")
        return None


def generate_e_id(fingerprint: str | None = None) -> str:
    """
    生成 e_id: 使用 mmh3.hash128, seed=31, 转成 32 位十六进制字符串。
    fingerprint 若未提供, 默认使用随机 token。
    """
    fp = fingerprint or secrets.token_hex(16)
    hashed = mmh3.hash128(fp, seed=31, signed=False)
    return format(hashed, "032x")


def compute_sign(img_bytes: bytes, timestamp_ms: int | None = None) -> tuple[str, int]:
    """
    sign = Base64( AES-CBC( pad(md5(file) + timestamp_ms), key, iv ) )
    返回 (sign, timestamp_ms)
    """
    ts = timestamp_ms if timestamp_ms is not None else int(time.time() * 1000)
    md5_hex = hashlib.md5(img_bytes).hexdigest()
    plaintext = (md5_hex + str(ts)).encode()
    cipher = AES.new(SIGN_KEY, AES.MODE_CBC, SIGN_IV)
    encrypted = cipher.encrypt(pad(plaintext, AES.block_size))
    sign = base64.b64encode(encrypted).decode()
    return sign, ts


async def fetch_benefit_status(e_id: str | None = DEFAULT_E_ID, product_id: str = DEFAULT_P_ID) -> dict:
    """获取权益状态"""
    e_id_final = e_id or generate_e_id()
    headers = _build_headers()
    payload = {"e_id": e_id_final, "product_id": product_id}

    logger.debug(f"Benefit status request headers: {headers}")
    logger.info(f"Benefit status payload: {payload}")

    client = await get_http_client()
    response = await client.post(
        BENEFIT_STATUS_ENDPOINT,
        data=payload,
        headers=headers,
    )
    response.raise_for_status()
    data = response.json()

    logger.info(f"Benefit status response: {data}")
    return data


async def fetch_benefit_status_safe(
    e_id: str | None = DEFAULT_E_ID, product_id: str = DEFAULT_P_ID
) -> dict | None:
    """获取权益状态（安全版本）"""
    try:
        return await fetch_benefit_status(e_id=e_id, product_id=product_id)
    except httpx.HTTPStatusError as exc:
        logger.warning(f"Benefit status HTTP error: {exc.response.status_code}")
        return None
    except Exception as exc:
        logger.warning(f"Benefit status request failed: {exc}")
        return None


async def upload_remove_wm(
    img_bytes: bytes,
    img_filename: str | None,
    img_content_type: str | None,
    mask_bytes: bytes | None,
    mask_filename: str | None,
    mask_content_type: str | None,
    sign: str | None,
    name: str | None,
    e_id: str,
) -> dict:
    """上传图片和掩膜到水印去除服务"""
    headers = _build_headers(content_type=None)
    logger.debug(f"Upload request headers: {headers}")

    files = [
        (
            "img",
            (
                img_filename or "img",
                img_bytes,
                img_content_type or "application/octet-stream",
            ),
        )
    ]
    if mask_bytes is not None:
        files.append(
            (
                "mask",
                (
                    mask_filename or "mask",
                    mask_bytes,
                    mask_content_type or "application/octet-stream",
                ),
            )
        )

    data = {}
    if sign is not None:
        data["sign"] = sign
    if name is not None:
        data["name"] = name
    data["e_id"] = e_id

    client = await get_http_client()
    # 上传操作使用更长的超时时间
    response = await client.post(
        REMOVE_WM_UPLOAD_ENDPOINT,
        headers=headers,
        data=data,
        files=files,
        timeout=UPLOAD_TIMEOUT,
    )
    response.raise_for_status()
    result = response.json()

    logger.info(
        f"Upload request summary: img_size={len(img_bytes)}, "
        f"mask_size={len(mask_bytes) if mask_bytes is not None else 0}, "
        f"sign_len={len(sign) if sign else 0}, name={name}, e_id={e_id}, "
        f"status={response.status_code}"
    )
    logger.debug(f"Upload response: {result}")
    return result


async def upload_remove_wm_safe(
    img_bytes: bytes,
    img_filename: str | None,
    img_content_type: str | None,
    mask_bytes: bytes | None,
    mask_filename: str | None,
    mask_content_type: str | None,
    sign: str | None,
    name: str | None,
    e_id: str,
) -> dict | None:
    """上传图片（安全版本）"""
    try:
        return await upload_remove_wm(
            img_bytes=img_bytes,
            img_filename=img_filename,
            img_content_type=img_content_type,
            mask_bytes=mask_bytes,
            mask_filename=mask_filename,
            mask_content_type=mask_content_type,
            sign=sign,
            name=name,
            e_id=e_id,
        )
    except httpx.HTTPStatusError as exc:
        logger.error(f"Upload HTTP error: {exc.response.status_code}, response: {exc.response.text}")
        return None
    except Exception as exc:
        logger.error(f"Upload request failed: {exc}", exc_info=True)
        return None


async def fetch_wm_status(token: str, e_id: str) -> dict:
    """查询水印处理状态"""
    headers = _build_headers(content_type=None)
    payload = {"token": token, "e_id": e_id}

    logger.info(f"WM status payload: {payload}")

    client = await get_http_client()
    response = await client.post(
        REMOVE_WM_STATUS_ENDPOINT,
        headers=headers,
        data=payload,
        timeout=UPLOAD_TIMEOUT,
    )
    response.raise_for_status()
    result = response.json()

    logger.debug(f"WM status response: {result}")
    return result


async def fetch_wm_status_safe(token: str | None, e_id: str | None) -> dict | None:
    """查询水印处理状态（安全版本）"""
    if not token or not e_id:
        logger.warning("WM status skipped: missing token or e_id")
        return None

    try:
        return await fetch_wm_status(token=token, e_id=e_id)
    except httpx.HTTPStatusError as exc:
        logger.error(f"WM status HTTP error: {exc.response.status_code}, response: {exc.response.text}")
        return None
    except Exception as exc:
        logger.error(f"WM status request failed: {exc}", exc_info=True)
        return None


async def fetch_remove_status(token: str) -> dict:
    """根据 token 查询擦除结果"""
    headers = _build_headers()
    payload = {"token": token}

    logger.info(f"Remove status payload: {payload}")

    client = await get_http_client()
    response = await client.post(
        REMOVE_WM_STATUS_POLL_ENDPOINT,
        headers=headers,
        data=payload,
        timeout=UPLOAD_TIMEOUT,
    )
    response.raise_for_status()
    result = response.json()

    logger.debug(f"Remove status response: {result}")
    return result


async def fetch_remove_status_safe(token: str | None) -> dict | None:
    """根据 token 查询擦除结果（安全版本）"""
    if not token:
        logger.warning("Remove status skipped: missing token")
        return None

    try:
        return await fetch_remove_status(token=token)
    except httpx.HTTPStatusError as exc:
        logger.error(
            f"Remove status HTTP error: {exc.response.status_code}, response: {exc.response.text}"
        )
        return None
    except Exception as exc:
        logger.error(f"Remove status request failed: {exc}", exc_info=True)
        return None
