import os
from pathlib import Path

from utils.config import Config
from xtractor.table import TableProcess


def process_table(
    table_file_folder: str | Path,
    file_path: str | Path,
    server: str,
    type: str,
    db_key: str | None = None,
):
    """
    JSON 数据提取/打包工具

    Args:
        table_file_folder: ExcelDB.db 和 Excel.zip 所在目录
        file_path: 文件输出路径 / 需打包的文件输入路径
        server: 服务器区域，可选 CN / GL / JP
        type: 操作类型，可选 Extract / Repack
        db_key: 数据库加解密密钥，可选
    """

    table_file_folder = Path(table_file_folder)
    file_path = Path(file_path)

    if server not in ("CN", "GL", "JP"):
        raise ValueError(f"不支持的 server: {server}")

    if type not in ("Extract", "Repack"):
        raise ValueError(f"不支持的 type: {type}")

    file_path.mkdir(parents=True, exist_ok=True)

    process = TableProcess(
        server,
        db_key,
        str(table_file_folder),
        str(file_path),
    )

    excel_db = table_file_folder / "ExcelDB.db"
    excel_zip = table_file_folder / "Excel.zip"

    if excel_db.exists():
        process.process_table("ExcelDB.db", type)

    if excel_zip.exists():
        process.process_table("Excel.zip", type)

