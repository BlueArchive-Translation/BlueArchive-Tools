import argparse
import json
import os
import re
import shutil
import tempfile
import time

from utils.download import ResourceDownloader
from utils.util import ZipUtils
from utils.git import Git
from utils.config import Config
from utils.server import SSHServer
from xtractor.bundle import BundleExtractor


class BundlePublisher:
    def __init__(self, server):
        self.server = server
        self.bundle_repositories = getattr(Config, f"Bundle_repositories_{server}")
        self.repo_root = tempfile.mkdtemp(prefix="bundle_")
        self.spine_roots = {}
        self.ssh_server = None
        self.rd = ResourceDownloader(server)
        self.extractor = BundleExtractor()
        self.data = self.load_json()
        self.temp_root = None
        self.git_state = None

    @staticmethod
    def is_texture_bundle(filename):
        """过滤非图片资源"""
        name = os.path.basename(filename).lower()
        return name.endswith(".bundle") and bool(re.search(r"(?:^|-)textures(?:-|_)", name))

    @staticmethod
    def spine_type(filename):
        """过滤角色立绘和记忆大厅"""
        name = os.path.basename(filename).lower()
        m = re.search(r"(?:^|-)spinecharacters-([^-]+)-", name)
        if m:
            return "spinecharacters", m.group(1)
        m = re.search(r"(?:^|-)spinelobbies-([^-]+)-", name)
        if m:
            return "spinelobbies", m.group(1)
        return None

    @staticmethod
    def bundle_regex(filename):
        """去除文件的时间和CRC，避免因更新导致无法匹配"""
        name = os.path.basename(filename)
        stem, ext = os.path.splitext(name)
        stem = re.sub(r"-\d{4}-\d{2}-\d{2}", "-{DATE}", stem)
        stem = re.sub(r"_\d+$", "_{CRC}", stem)
        pattern = re.escape(stem + ext)
        pattern = pattern.replace(re.escape("-{DATE}"), r"-\d{4}-\d{2}-\d{2}")
        pattern = pattern.replace(re.escape("_{CRC}"), r"_\d+")
        return f"^{pattern}$"

    @staticmethod
    def load_json():
        """加载config文件"""
        if not os.path.isfile(Config.bundle_config):
            return {}
        try:
            with open(Config.bundle_config, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def save_json(data):
        """保存config文件"""
        temp = f"{Config.bundle_config}.tmp"
        with open(temp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, Config.bundle_config)

    @staticmethod
    def build_server_data():
        """创建服务器配置"""
        return {"FullPatchPacks": [], "UpdatePacks": []}

    @staticmethod
    def find_bundle(extract_dir, filename):
        """寻找bundle文件"""
        for root, _, files in os.walk(extract_dir):
            if filename in files:
                return os.path.join(root, filename)
        return None

    @staticmethod
    def scan_bundle(extractor, bundle_path, data_type):
        """扫描bundle资源"""
        try:
            result = extractor.search_unity_pack(bundle_path, data_type=[data_type], collect_only=False)
        except Exception:
            return {}
        assets = {}
        for obj in result:
            info = getattr(obj, "_info", {})
            name = info.get("name", "")
            if name:
                assets[name] = {"PathID": info.get("path_id", ""), "Entry": info.get("entry", "")}
        return assets

    @staticmethod
    def extract_bundle_type(extractor, bundle_path, output_dir, data_type):
        """提取类型设置"""
        os.makedirs(output_dir, exist_ok=True)
        old_dir = extractor.BUNDLE_EXTRACT_FOLDER
        extractor.BUNDLE_EXTRACT_FOLDER = output_dir
        try:
            extractor.extract_bundle(bundle_path, [data_type])
        finally:
            extractor.BUNDLE_EXTRACT_FOLDER = old_dir

    @staticmethod
    def flatten_spine_output(output):
        """将TextAsset和Texture2D中的文件移动到角色目录"""
        for resource_type in ("TextAsset", "Texture2D"):
            resource_dir = os.path.join(output, resource_type)
            if not os.path.isdir(resource_dir):
                continue
            for root, _, files in os.walk(resource_dir):
                for filename in files:
                    source = os.path.join(root, filename)
                    target = os.path.join(output, filename)
                    if os.path.exists(target):
                        os.remove(target)
                    shutil.move(source, target)
            shutil.rmtree(resource_dir, ignore_errors=True)

    def upload_spine_output(self, output, spine_type_value, name):
        """上传Spine资源"""
        remote_root = f"/var/www/web/{spine_type_value}"
        remote_path = f"{remote_root}/{name}"
        if os.path.isdir(output):
            self.ssh_server.upload(output, remote_path, recursive=True)
            print(f"[Server] 上传 Spine：{remote_path}")

    def extract_spine_bundle(self, bundle_path, spine_type_value, name, output_root):
        """提取并上传spine"""
        output = os.path.join(output_root, name)
        textassets = self.scan_bundle(self.extractor, bundle_path, "TextAsset")
        textures = self.scan_bundle(self.extractor, bundle_path, "Texture2D")
        if textassets or textures:
            self.extract_bundle_type(self.extractor, bundle_path, output, "TextAsset")
            self.extract_bundle_type(self.extractor, bundle_path, output, "Texture2D")
            self.flatten_spine_output(output)
            self.upload_spine_output(output, spine_type_value, name)
        return textassets, textures

    def extract_texture_bundle(self, bundle_path, zip_name, bundle_name, output_root):
        """提取Texture2D"""
        zip_stem = os.path.splitext(os.path.basename(zip_name))[0]
        bundle_stem = os.path.splitext(os.path.basename(bundle_name))[0]
        output = os.path.join(output_root, zip_stem, bundle_stem)
        self.extract_bundle_type(self.extractor, bundle_path, output, "Texture2D")

    @staticmethod
    def build_bundle_record(bundle):
        return {"Name": bundle.get("Name", ""), "Size": bundle.get("Size", 0), "Crc": bundle.get("Crc", 0), "Textures2D": {}}

    def build_bundle_record_with_assets(self, bundle, extract_dir, zip_name, output_root):
        name = bundle.get("Name", "")
        record = self.build_bundle_record(bundle)
        record["Name"] = self.bundle_regex(name)
        if not name:
            return record
        bundle_path = self.find_bundle(extract_dir, name)
        if not bundle_path:
            return record

        # 只有JP服务器处理Spine
        if self.server == "JP":
            spine_info = self.spine_type(name)
            if spine_info:
                spine_type_value, spine_name = spine_info
                spine_output_root = self.spine_roots[spine_type_value]
                textassets, textures = self.extract_spine_bundle(bundle_path, spine_type_value, spine_name, spine_output_root)
                record["TextAssets"] = textassets
                record["Textures2D"] = textures
                return record

        if not self.is_texture_bundle(name):
            return record
        textures = self.scan_bundle(self.extractor, bundle_path, "Texture2D")
        record["Textures2D"] = textures
        if textures:
            self.extract_texture_bundle(bundle_path, zip_name, name, output_root)
        return record

    def download_pack(self, pack_name, zip_dir):
        while True:
            result = self.rd.get_bundle_files([pack_name], save_path=zip_dir, workers=1).get(pack_name)
            if result is not False and result is not None:
                return os.path.join(zip_dir, pack_name)
            print(f"[Download] {pack_name} 暂未开放")
            time.sleep(30)

    @staticmethod
    def build_pack_record(pack):
        return {"PackName": pack.get("PackName", ""), "PackSize": pack.get("PackSize", 0), "Crc": pack.get("Crc", 0), "BundleFiles": []}

    @staticmethod
    def is_pack_processed(pack_record, pack):
        """判断Pack是否已经处理过"""
        return pack_record.get("PackName", "") == pack.get("PackName", "") and pack_record.get("PackSize", 0) == pack.get("PackSize", 0) and pack_record.get("Crc", 0) == pack.get("Crc", 0)

    def find_processed_pack(self, pack_type, pack):
        """查找已经处理过的Pack"""
        for pack_record in self.data.get(self.server, {}).get(pack_type, []):
            if self.is_pack_processed(pack_record, pack):
                return pack_record
        return None

    def process_pack(self, pack, pack_type, index, total):
        pack_name = pack["PackName"]
        print(f"[ZIP] {index}/{total} {pack_name}")
        zip_dir = tempfile.mkdtemp(prefix="zip_", dir=self.temp_root)
        extract_dir = tempfile.mkdtemp(prefix="extract_", dir=self.temp_root)
        try:
            zip_path = self.download_pack(pack_name, zip_dir)
            ZipUtils.extract_zip(zip_path, extract_dir, progress_bar=False)
            pack_record = self.find_processed_pack(pack_type, pack)
            if pack_record:
                self.data[self.server][pack_type].remove(pack_record)
            pack_record = self.build_pack_record(pack)
            self.data[self.server][pack_type].append(pack_record)
            self.save_json(self.data)
            for bundle in pack.get("BundleFiles", []):
                record = self.build_bundle_record_with_assets(bundle, extract_dir, pack_name, self.repo_root)
                pack_record["BundleFiles"].append(record)
                self.save_json(self.data)
                self.git_state["pending"] += len(record.get("Textures2D", {}))
                if self.git_state["pending"] >= 200:
                    self.git_state["git"].add(".")
                    if self.git_state["git"].has_staged_changes():
                        self.git_state["git"].commit(f"Update resources ({self.git_state['pending']} images)")
                        self.git_state["git"].push()
                        print(f"[Git] 提交 {self.git_state['pending']} 个新增图片")
                    self.git_state["pending"] = 0
            print(f"[ZIP] 完成：{pack_name}")
        finally:
            shutil.rmtree(zip_dir, ignore_errors=True)
            shutil.rmtree(extract_dir, ignore_errors=True)

    def init(self):
        Git().clone(self.bundle_repositories, self.repo_root)

        # 只有JP服务器处理Spine
        if self.server == "JP":
            self.spine_roots = {
                "spinecharacters": tempfile.mkdtemp(prefix="spine_characters_"),
                "spinelobbies": tempfile.mkdtemp(prefix="spine_lobbies_"),
            }
            self.ssh_server = SSHServer(
                host=os.environ["SERVER_HOST"],
                username="root",
                password=os.environ["SERVER_PASSWORD"],
                port=22,
            )

        self.data.setdefault(self.server, self.build_server_data())

        bp = self.rd.get_bundle_packing()
        if not bp:
            print("[Init] 获取 BundlePacking 失败")
            raise SystemExit(1)

        self.save_json(self.data)
        self.temp_root = tempfile.mkdtemp(prefix="bundle_download_")
        self.git_state = {"git": Git(self.repo_root), "pending": 0}
        return bp

    def commit(self):
        self.git_state["git"].add(".")
        if self.git_state["git"].has_staged_changes():
            self.git_state["git"].commit(f"Update resources ({self.git_state['pending']} images)")
            print(f"[Git] 最终提交 {self.git_state['pending']} 个新增图片")

        project_git = Git(os.getcwd())
        project_git.pull()
        project_git.add(Config.bundle_config)
        if project_git.has_staged_changes():
            project_git.commit("Update bundle config")
            print(f"[Git] 已提交 BundleConfig：{Config.bundle_config}")

    def cleanup(self):
        if self.temp_root:
            shutil.rmtree(self.temp_root, ignore_errors=True)
        if self.repo_root:
            shutil.rmtree(self.repo_root, ignore_errors=True)
        for spine_root in self.spine_roots.values():
            shutil.rmtree(spine_root, ignore_errors=True)

    def run(self):
        bp = self.init()
        try:
            groups = [("FullPatchPacks", bp.get("FullPatchPacks", [])), ("UpdatePacks", bp.get("UpdatePacks", []))]
            total = sum(len(packs) for _, packs in groups)
            index = 0
            for pack_type, packs in groups:
                for pack in packs:
                    if not pack.get("PackName"):
                        continue
                    if self.find_processed_pack(pack_type, pack):
                        print(f"[Skip] {pack['PackName']} 已处理")
                        continue
                    index += 1
                    self.process_pack(pack, pack_type, index, total)
            self.commit()
        finally:
            self.cleanup()
