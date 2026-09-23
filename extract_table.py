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

def prepare_flatdata(server):
    print("正在克隆 FlatData 仓库...")

    Git().clone(
        Config.FlatData_repositories,
        Config.FlatData_path
    )

    git = Git(Config.FlatData_path)
    git.checkout(server)

    print(f"FlatData 已切换到 {server} 分支。")

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


def find_large_files(root_path, paths, min_size=100 * 1024 * 1024):
    files = []

    for relative_path in paths:
        target_path = os.path.join(root_path, relative_path)

        if not os.path.exists(target_path):
            continue

        for root, _, filenames in os.walk(target_path):
            for filename in filenames:
                file_path = os.path.join(root, filename)

                if os.path.getsize(file_path) > min_size:
                    files.append(os.path.relpath(file_path, root_path))

    return files


def publish_table_bundles(server, version_name, zip_path, output_path):
    repo_path = tempfile.mkdtemp(prefix="TableBundles_")
    try:
        print("正在克隆 TableBundles 仓库...")
        Git().clone(Config.TableBundles_repositories, repo_path)

        git = Git(repo_path)

        git.checkout("main")

        shutil.copy2(
            zip_path,
            os.path.join(repo_path, os.path.basename(zip_path))
        )

        git.add(os.path.basename(zip_path))

        if git.has_staged_changes():
            git.commit(f"Update Table {version_name}")
            git.push("main")
            print("main 分支提交完成。")
        else:
            print("main 分支没有需要提交的修改。")

        git.checkout(server)

        for name in ("Excel", "ExcelDB"):
            source = os.path.join(output_path, name)
            target = os.path.join(repo_path, name)

            if os.path.exists(target):
                if os.path.isdir(target):
                    shutil.rmtree(target)
                else:
                    os.remove(target)

            shutil.copytree(source, target)

        large_files = find_large_files(
            repo_path,
            ["Excel", "ExcelDB"]
        )

        if large_files:
            git.lfs_install()

            for file_path in large_files:
                print(f"启用 Git LFS: {file_path}")
                git.lfs_track(file_path)

            git.add(".gitattributes")

        git.add("Excel")
        git.add("ExcelDB")

        if git.has_staged_changes():
            git.commit(f"Update Table {version_name}")
            git.push(server)
            print(f"{server} 分支提交完成。")
        else:
            print(f"{server} 分支没有需要提交的修改。")
    finally:
        shutil.rmtree(repo_path, ignore_errors=True)
        print("TableBundles 临时仓库已删除。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("server", choices=["JP", "GL", "CN"], help="选择服务器区域")
    parser.add_argument("region", nargs="?", choices=["kr", "tw", "asia", "na", "global"], default="na", help="GL区服")
    args = parser.parse_args()

    prepare_api(args.server)

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

        prepare_flatdata(args.server)

        table = TableExtract(
            server=args.server,
            password=key,
            table_file_folder=temp_path,
            extract_folder=output_path,
            flat_data_module_name=Config.FlatData
        )

        table.extract_db_file("ExcelDB.db"):

        table.extract_zip_file("Excel.zip")

        print(f"Table处理完成: {output_path}")

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

        publish_table_bundles(
            args.server,
            version_name,
            zip_path,
            output_path
        )
    finally:
        shutil.rmtree(temp_path, ignore_errors=True)
        shutil.rmtree(output_path, ignore_errors=True)
        print("临时文件夹已删除。")
