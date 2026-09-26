import argparse
import os
import shutil
import tempfile

from utils.config import Config
from utils.git import Git
from utils.regions import Server
from utils.util import ZipUtils
from xtractor.table import TableTask


class TablePublisher:
    def __init__(self, server, region="na"):
        self.server = server
        self.region = region
        self.task = TableTask(server=server, region=region, dispatch_type="Extract")

    @staticmethod
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

    def publish_table_bundles(self, version_name, zip_path, output_path):
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

            git.checkout(self.server)

            for name in ("Excel", "ExcelDB"):
                source = os.path.join(output_path, name)
                target = os.path.join(repo_path, name)
                if os.path.exists(target):
                    if os.path.isdir(target):
                        shutil.rmtree(target)
                    else:
                        os.remove(target)
                shutil.copytree(source, target)

            large_files = self.find_large_files(
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
                git.push(self.server)
                print(f"{self.server} 分支提交完成。")
            else:
                print(f"{self.server} 分支没有需要提交的修改。")
        finally:
            shutil.rmtree(repo_path, ignore_errors=True)
            print("TableBundles 临时仓库已删除。")

    def run(self):
        try:
            table = self.task.run()
            if table is None:
                raise SystemExit(1)

            table.extract_db_file("ExcelDB.db")
            table.extract_zip_file("Excel.zip")

            print(f"Table处理完成: {self.task.output_path}")

            server = Server(self.server)
            version_name = server.get_version_name(
                is_full_name=True,
                target_version_key="TableVersion"
            )
            zip_path = os.path.join(".", f"{version_name}.zip")

            ZipUtils.create_zip(
                ["Excel", "ExcelDB"],
                zip_path,
                base_dir=self.task.output_path
            )

            print(f"打包完成: {zip_path}")

            self.publish_table_bundles(
                version_name,
                zip_path,
                self.task.output_path
            )
            return version_name
        finally:
            self.task.cleanup()

