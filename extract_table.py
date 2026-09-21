import argparse
import os
import secrets
import shutil
import tempfile
import time

from utils.download import ResourceDownloader
from utils.config import Config
from utils.git import Git
from utils.regions import Server
from utils.util import ZipUtils
from xtractor.table import TableExtract


def prepare_api(server):
    if server not in ("JP", "GL"):
        return

    required_dirs = ("lib", "code", "request_api")

    if all(os.path.isdir(name) for name in required_dirs):
        print("API依赖已存在，跳过克隆。")
        return

    temp_path = tempfile.mkdtemp(prefix="API_")
    try:
        Git().clone(Config.API_repositories, temp_path)

        for name in required_dirs:
            source = os.path.join(temp_path, name)
            target = os.path.join(".", name)

            if not os.path.exists(source):
                raise RuntimeError(f"API仓库缺少目录: {name}")

            if os.path.exists(target):
                if os.path.isdir(target):
                    shutil.rmtree(target)
                else:
                    os.remove(target)

            shutil.move(source, target)
    finally:
        shutil.rmtree(temp_path, ignore_errors=True)


def get_key(server, region=None):
    from request_api.YostarAPI.QueuingAPI import QueuingAPI as YostarQueuingAPI
    from request_api.NexonAPI.QueuingAPI import QueuingAPI as NexonQueuingAPI

    apk_url, game_version = Server(server).get_apk_url()

    if server == "JP":
        server_config = Config.servers[server]
        public_key = Config.YOSTAR_PUBLIC_KEY.encode()
        api_class = YostarQueuingAPI
    else:
        server_config = Config.get_region_config(region)
        public_key = Config.NEXON_PUBLIC_KEY.encode()
        api_class = NexonQueuingAPI

    key = secrets.token_bytes(16)
    iv = secrets.token_bytes(16)

    api = api_class(server_config["gateway_url"], key, iv, public_key)

    if server == "JP":
        result = api.Queuing_GetAuthTicket(
            YostarUID=33027791391,
            YostarToken=os.getenv("YostarToken"),
            ClientVersion=game_version
        )
    else:
        result = api.Queuing_GetCryptoKeys()

    print(f"服务器: {server}")
    print(f"密钥: {result['EncryptedSqlCipherKey']}")
    return result["EncryptedSqlCipherKey"]


def download_table(download, temp_path):
    files = download.get_table_files(
        ["ExcelDB.db", "Excel.zip"],
        save_path=temp_path,
        workers=2
    )

    if files is None:
        raise RuntimeError("下载地址不存在。")

    if any(value is None for value in files.values()):
        raise RuntimeError(f"下载地址不存在: {files}")

    if all(value is True for value in files.values()):
        return True

    if any(value is False for value in files.values()):
        return False

    raise RuntimeError(f"未知的下载状态: {files}")


def extract_table(server, key, temp_path, output_path):
    table = TableExtract(
        server=server,
        password=key,
        table_file_folder=temp_path,
        extract_folder=output_path,
        flat_data_module_name=Config.FlatData
    )

    if not table.extract_db_file("ExcelDB.db"):
        raise RuntimeError("ExcelDB.db 处理失败。")

    table.extract_zip_file("Excel.zip")

    print(f"Table处理完成: {output_path}")
    return output_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("server", choices=["JP", "GL", "CN"], help="选择服务器区域")
    parser.add_argument("region", nargs="?", choices=["kr", "tw", "asia", "na", "global"], default="na", help="GL区服")
    args = parser.parse_args()

    prepare_api()

    key = None
    if args.server in ("JP", "GL"):
        key = get_key(args.server, args.region)

    download = ResourceDownloader(args.server)
    server = Server(args.server)

    temp_path = tempfile.mkdtemp(prefix="Download_")
    output_path = tempfile.mkdtemp(prefix="Output_")

    try:
        start_time = time.time()
        timeout = 5 * 60 * 60

        while time.time() - start_time < timeout:
            result = download_table(download, temp_path)
            if result is True:
                print("下载完成。")
                break

            print("服务器暂未开放，30秒后重试。")
            time.sleep(30)
        else:
            print("等待超时，触发 Extract。")
            Git().dispatch("Extract", {"type": "Table"})
            print("检查结束，程序退出。")
            raise SystemExit(1)

        extract_table(args.server, key, temp_path, output_path)

        version_name = server.get_version_name(
            is_full_name=True,
            target_version_key="TableVersion"
        )
        zip_path = os.path.join(".", f"{version_name}.zip")

        ZipUtils.create_zip(
            ["Excel", "ExcelDB"],
            zip_path,
            base_dir=output_path
        )

        print(f"打包完成: {zip_path}")
    finally:
        shutil.rmtree(temp_path, ignore_errors=True)
        shutil.rmtree(output_path, ignore_errors=True)
        print("临时文件夹已删除。")




