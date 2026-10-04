import json
import os
import re
import shutil
import tempfile

from utils.download import ResourceDownloader
from utils.util import ZipUtils
from utils.git import Git
from utils.config import Config
from utils.server import SSHServer
from xtractor.bundle import BundleExtractor


spine_bundle = re.compile(r"(spinecharacters|spinelobbies)", re.I)
spine_logical_name = re.compile(r"^(.*?)-(?:textures|textassets)-.*?\.bundle$", re.I)
spine_name = re.compile(r"(?:spinecharacters|spinelobbies)-([^-]+)-", re.I)


class BundlePublisher:
    def __init__(self, server):
        self.server = server
        self.repo_dir = f"BA-Bundles-Extract-{server}"
        self.config = {}
        self.repo_git = Git(self.repo_dir)
        self.git = Git()
        self.ssh = SSHServer(
            host=os.environ["SERVER_HOST"],
            username="root",
            password=os.environ["SERVER_PASSWORD"],
            port=22,
        )
        self.downloader = ResourceDownloader(server, verbose=True)
        self.bundle_extractor = BundleExtractor()

    def _logical_name(self, filename):
        match = spine_logical_name.match(filename)
        if match:
            return match.group(1)
        return re.sub(r"_\d+\.bundle$", "", filename, flags=re.I)

    def _special_info(self, filename):
        match = spine_bundle.search(filename)
        role = spine_name.search(filename)
        if not match or not role:
            return None
        return match.group(1).lower(), role.group(1)

    def _build_groups(self, bundle_files, zip_root):
        groups = {}

        for bundle in bundle_files:
            filename = bundle.get("Name", "")

            if "textures" in filename:
                extract_type = "Texture2D"
                resource_type = "textures"
            elif "textassets" in filename and spine_bundle.search(filename):
                extract_type = "TextAsset"
                resource_type = "textassets"
            else:
                continue

            path = os.path.join(zip_root, filename)
            if not os.path.isfile(path):
                print(f"[跳过] ZIP 中不存在 Bundle: {filename}")
                continue

            logical_name = self._logical_name(filename)
            group = groups.setdefault(
                logical_name,
                {
                    "name": logical_name,
                    "special": self._special_info(filename),
                    "sources": [],
                },
            )
            group["sources"].append({
                "path": path,
                "name": filename,
                "size": bundle.get("Size", 0),
                "crc": bundle.get("Crc", 0),
                "type": resource_type,
                "extract_type": extract_type,
            })

        print(f"[扫描] 找到 {len(groups)} 个 Bundle 分组，共 {sum(len(group['sources']) for group in groups.values())} 个 Bundle")
        return groups

    def _is_changed(self, group):
        old = self.config.get(group["name"])
        if not old:
            return True

        old_sources = old.get("sources", {})
        if len(old_sources) != len(group["sources"]):
            return True

        return any(
            old_sources.get(source["name"], {}).get("size") != source["size"]
            or old_sources.get(source["name"], {}).get("crc") != source["crc"]
            for source in group["sources"]
        )

    def _merge(self, source_root, target_root):
        for root, _, files in os.walk(source_root):
            relative = os.path.relpath(root, source_root)
            target = target_root if relative == "." else os.path.join(target_root, relative)
            os.makedirs(target, exist_ok=True)

            for name in files:
                src = os.path.join(root, name)
                dst = os.path.join(target, name)
                if os.path.exists(dst):
                    os.remove(dst)
                shutil.move(src, dst)

    def _upload_special(self, local_root, special):
        if not special:
            return

        category, role = special
        remote = os.path.join("/var/www/web", category, role)

        print(f"[上传] 特殊资源: {category}/{role}")
        self.ssh.remove_dir(remote)
        self.ssh.upload_directory(local_root, remote, create_parent=True)

    def _extract_group(self, group, zip_name):
        target_root = os.path.join(self.repo_dir, zip_name, group["name"])

        print(f"[提取] {group['name']} -> {zip_name}/{group['name']}")

        shutil.rmtree(target_root, ignore_errors=True)
        os.makedirs(target_root, exist_ok=True)

        resources = []

        for source in group["sources"]:
            temp_root = tempfile.mkdtemp(prefix="bundle_extract_")

            try:
                print(f"[提取] Bundle: {source['name']}")

                self.bundle_extractor.extract_bundle(
                    source["path"],
                    extract_types=[source["extract_type"]],
                    extract_root=temp_root,
                    use_type_subdir=False,
                )

                self._merge(temp_root, target_root)

                for root, _, files in os.walk(temp_root):
                    for name in files:
                        path = os.path.join(root, name)
                        resources.append({
                            "name": os.path.relpath(path, temp_root).replace("\\", "/"),
                            "size": os.path.getsize(path),
                        })
            finally:
                shutil.rmtree(temp_root, ignore_errors=True)

        print(f"[完成] {group['name']}，资源 {len(resources)} 个")

        self.config[group["name"]] = {
            "sources": {
                source["name"]: {
                    "size": source["size"],
                    "crc": source["crc"],
                }
                for source in group["sources"]
            },
            "resources": resources,
        }

        self._upload_special(target_root, group["special"])

    def _commit_resources(self, bundle_count=0):
        self.repo_git.add(".")

        if not self.repo_git.has_changes():
            print("[Git] 没有文件变化，跳过提交")
            return

        message = f"Update bundles ({bundle_count} bundles)"
        print(f"[Git] 提交: {message}")

        self.repo_git.commit(message)
        self.repo_git.push()
        print("[Git] 推送完成")

    def _process_zip(self, zip_path, pack):
        zip_root = tempfile.mkdtemp(prefix="bundle_zip_")
        zip_name = os.path.splitext(os.path.basename(zip_path))[0]

        print(f"[ZIP] 开始处理: {zip_name}")

        try:
            ZipUtils.extract_zip(zip_path, zip_root)

            groups = self._build_groups(
                pack.get("BundleFiles", []),
                zip_root,
            )

            processed_count = 0
            changed_count = 0

            for group in groups.values():
                if self._is_changed(group):
                    print(f"[变化] {group['name']}")
                    self._extract_group(group, zip_name)
                    processed_count += len(group["sources"])
                    changed_count += 1
                else:
                    print(f"[跳过] 无变化: {group['name']}")

            print(
                f"[ZIP] 处理完成: {zip_name}，"
                f"变化分组 {changed_count} 个，"
                f"处理 Bundle {processed_count} 个"
            )

            return processed_count
        finally:
            shutil.rmtree(zip_root, ignore_errors=True)

    def _download_pack(self, pack):
        filename = pack["PackName"]
        temp_dir = tempfile.mkdtemp(prefix="patch_pack_")

        print(f"[下载] {filename}")

        try:
            self.downloader.get_bundle_files(
                [filename],
                save_path=temp_dir,
            )

            path = os.path.join(temp_dir, filename)

            if not os.path.isfile(path):
                raise FileNotFoundError(f"Pack download failed: {path}")

            print(f"[下载] 完成: {filename}")
            return path, temp_dir
        except Exception:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise

    def _process_packs(self, catalog):
        packs = catalog.get("FullPatchPacks", []) + catalog.get("UpdatePacks", [])
        seen = set()

        print(f"[任务] 共发现 {len(packs)} 个 Pack")

        for index, pack in enumerate(packs, 1):
            name = pack.get("PackName")

            if name in seen:
                print(f"[跳过] 重复 Pack: {name}")
                continue

            seen.add(name)
            print(f"[Pack] [{index}/{len(packs)}] {name}")

            zip_path, temp_dir = self._download_pack(pack)

            try:
                bundle_count = self._process_zip(zip_path, pack)
                self._commit_resources(bundle_count)
            finally:
                shutil.rmtree(temp_dir, ignore_errors=True)

    def run(self):
        try:
            try:
                with open(Config.bundle_config, "r", encoding="utf-8") as f:
                    self.config = json.load(f)
                print(f"[配置] 已加载 Bundle 配置，共 {len(self.config)} 个记录")
            except (FileNotFoundError, json.JSONDecodeError):
                self.config = {}
                print("[配置] 未找到有效配置，按首次处理执行")

            repo_url = Config.Bundle_repositories.format(server=self.server)
            print(f"[Git] 克隆仓库: {repo_url}")
            self.git.clone(repo_url, self.repo_dir)

            catalog = self.downloader.get_bundle_packing()
            self._process_packs(catalog)

            self._commit_resources()

            with open(Config.bundle_config, "w", encoding="utf-8") as f:
                json.dump(
                    self.config,
                    f,
                    ensure_ascii=False,
                    indent=2,
                )

            print("[配置] Bundle 配置已更新")

            self.git.add(Config.bundle_config)

            if self.git.has_changes():
                print("[Git] 提交 Bundle 配置")
                self.git.commit("Update bundle config")
                self.git.push()
                print("[Git] Bundle 配置推送完成")
            else:
                print("[Git] Bundle 配置没有变化，跳过提交")
        finally:
            print("[结束] 清理临时资源")
            self.ssh.close()
            shutil.rmtree(self.repo_dir, ignore_errors=True)
