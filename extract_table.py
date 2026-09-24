import argparse
import os

from utils.config import Config
from utils.git import Git
from utils.regions import Server
from utils.util import ZipUtils
from xtractor.table import TableTask


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

    task = TableTask(
        server=args.server,
        region=args.region,
        dispatch_type="Repack"
    )

    try:
        table = task.run()

        if table is None:
            raise SystemExit(1)

        table.extract_db_file("ExcelDB.db")

        table.extract_zip_file("Excel.zip")

        print(f"Table处理完成: {task.output_path}")

        server = Server(args.server)

        version_name = server.get_version_name(
            is_full_name=True,
            target_version_key="TableVersion"
        )
        zip_path = os.path.join(".", f"{version_name}.zip")

        ZipUtils.create_zip(
            ["Excel", "ExcelDB"],
            zip_path,
            base_dir=task.output_path
        )

        print(f"打包完成: {zip_path}")

        publish_table_bundles(
            args.server,
            version_name,
            zip_path,
            task.output_path
        )
    finally:
        task.cleanup()
