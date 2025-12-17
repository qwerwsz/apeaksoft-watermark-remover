"""数据库模块 - 用于存储API调用记录"""
import logging
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator, Optional

import aiosqlite

logger = logging.getLogger(__name__)

# 数据库文件路径
DB_DIR = Path("data")
DB_DIR.mkdir(parents=True, exist_ok=True)
DB_FILE = DB_DIR / "api_calls.db"


async def init_database() -> None:
    """初始化数据库,创建表结构"""
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS api_calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ip_address TEXT NOT NULL,
                user_agent TEXT NOT NULL,
                image_filename TEXT,
                image_data BLOB NOT NULL,
                image_content_type TEXT,
                image_size_bytes INTEGER,
                image_width INTEGER,
                image_height INTEGER,
                token TEXT NOT NULL,
                e_id TEXT NOT NULL,
                result_url TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 创建索引以提高查询效率
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_token ON api_calls(token)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_e_id ON api_calls(e_id)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_ip_address ON api_calls(ip_address)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_created_at ON api_calls(created_at)
        """)

        await db.commit()
        logger.info(f"数据库已初始化: {DB_FILE}")


async def insert_api_call(
    ip_address: str,
    user_agent: str,
    image_filename: Optional[str],
    image_data: bytes,
    image_content_type: Optional[str],
    image_size_bytes: Optional[int],
    image_width: Optional[int],
    image_height: Optional[int],
    token: str,
    e_id: str,
    result_url: Optional[str] = None,
) -> int:
    """
    插入API调用记录

    Args:
        ip_address: 客户端IP地址
        user_agent: 客户端User-Agent
        image_filename: 图片文件名
        image_data: 图片完整二进制数据
        image_content_type: 图片MIME类型
        image_size_bytes: 图片大小(字节)
        image_width: 图片宽度
        image_height: 图片高度
        token: 处理任务Token
        e_id: 设备/请求ID
        result_url: 处理结果图片URL (可选,稍后更新)

    Returns:
        插入记录的ID
    """
    async with aiosqlite.connect(DB_FILE) as db:
        cursor = await db.execute(
            """
            INSERT INTO api_calls (
                ip_address, user_agent, image_filename, image_data,
                image_content_type, image_size_bytes, image_width,
                image_height, token, e_id, result_url
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ip_address,
                user_agent,
                image_filename,
                image_data,
                image_content_type,
                image_size_bytes,
                image_width,
                image_height,
                token,
                e_id,
                result_url,
            ),
        )
        await db.commit()
        record_id = cursor.lastrowid
        logger.info(
            f"插入API调用记录: id={record_id}, ip={ip_address}, "
            f"token={token}, e_id={e_id}, image_size={image_size_bytes}"
        )
        return record_id


async def update_result_url(token: str, result_url: str) -> bool:
    """
    更新处理结果URL

    Args:
        token: 处理任务Token
        result_url: 处理结果图片URL

    Returns:
        是否更新成功
    """
    async with aiosqlite.connect(DB_FILE) as db:
        cursor = await db.execute(
            """
            UPDATE api_calls
            SET result_url = ?, updated_at = CURRENT_TIMESTAMP
            WHERE token = ?
            """,
            (result_url, token),
        )
        await db.commit()
        rows_affected = cursor.rowcount

        if rows_affected > 0:
            logger.info(f"更新结果URL成功: token={token}, url={result_url}")
            return True
        else:
            logger.warning(f"更新结果URL失败: 未找到token={token}的记录")
            return False


async def get_api_call_by_token(token: str) -> Optional[dict]:
    """
    根据token查询API调用记录

    Args:
        token: 处理任务Token

    Returns:
        API调用记录字典,如果未找到返回None
    """
    async with aiosqlite.connect(DB_FILE) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM api_calls WHERE token = ? ORDER BY created_at DESC LIMIT 1",
            (token,),
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return dict(row)
            return None


async def get_api_calls_by_ip(ip_address: str, limit: int = 100) -> list[dict]:
    """
    根据IP地址查询API调用记录

    Args:
        ip_address: 客户端IP地址
        limit: 返回记录数量限制

    Returns:
        API调用记录列表
    """
    async with aiosqlite.connect(DB_FILE) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM api_calls WHERE ip_address = ? ORDER BY created_at DESC LIMIT ?",
            (ip_address, limit),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


async def get_recent_api_calls(limit: int = 100) -> list[dict]:
    """
    获取最近的API调用记录

    Args:
        limit: 返回记录数量限制

    Returns:
        API调用记录列表
    """
    async with aiosqlite.connect(DB_FILE) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM api_calls ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


async def get_statistics() -> dict:
    """
    获取API调用统计信息

    Returns:
        统计信息字典
    """
    async with aiosqlite.connect(DB_FILE) as db:
        # 总调用次数
        async with db.execute("SELECT COUNT(*) as total FROM api_calls") as cursor:
            row = await cursor.fetchone()
            total_calls = row[0] if row else 0

        # 成功处理的次数(有result_url的)
        async with db.execute(
            "SELECT COUNT(*) as success FROM api_calls WHERE result_url IS NOT NULL"
        ) as cursor:
            row = await cursor.fetchone()
            success_calls = row[0] if row else 0

        # 独立IP数量
        async with db.execute(
            "SELECT COUNT(DISTINCT ip_address) as unique_ips FROM api_calls"
        ) as cursor:
            row = await cursor.fetchone()
            unique_ips = row[0] if row else 0

        # 今日调用次数
        async with db.execute(
            "SELECT COUNT(*) as today FROM api_calls WHERE DATE(created_at) = DATE('now')"
        ) as cursor:
            row = await cursor.fetchone()
            today_calls = row[0] if row else 0

        return {
            "total_calls": total_calls,
            "success_calls": success_calls,
            "unique_ips": unique_ips,
            "today_calls": today_calls,
            "success_rate": f"{(success_calls / total_calls * 100):.2f}%" if total_calls > 0 else "0%",
        }


async def get_image_data(record_id: int) -> Optional[tuple[bytes, str, str]]:
    """
    根据记录ID获取图片数据

    Args:
        record_id: 记录ID

    Returns:
        (图片二进制数据, 内容类型, 文件名) 如果未找到返回None
    """
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT image_data, image_content_type, image_filename FROM api_calls WHERE id = ?",
            (record_id,),
        ) as cursor:
            row = await cursor.fetchone()
            if row and row[0]:
                return (row[0], row[1] or "application/octet-stream", row[2] or f"image_{record_id}")
            return None


async def get_image_data_by_token(token: str) -> Optional[tuple[bytes, str, str]]:
    """
    根据token获取图片数据

    Args:
        token: 处理任务Token

    Returns:
        (图片二进制数据, 内容类型, 文件名) 如果未找到返回None
    """
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT image_data, image_content_type, image_filename FROM api_calls WHERE token = ? ORDER BY created_at DESC LIMIT 1",
            (token,),
        ) as cursor:
            row = await cursor.fetchone()
            if row and row[0]:
                return (row[0], row[1] or "application/octet-stream", row[2] or f"image_{token}")
            return None

