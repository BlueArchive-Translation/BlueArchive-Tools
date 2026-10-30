import hashlib
import json
import os
import re
import shlex
import shutil
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

from utils.download import ResourceDownloader
from utils.server import SSHServer
from utils.util import ZipUtils
from xtractor.bundle import BundleExtractor

REMOTE_ROOT = "/var/www/ba-all-data/UIs/03_Scenario"
REMOTE_CATEGORY_ROOTS = {
    "spinecharacters": f"{REMOTE_ROOT}/02_Character",
    "spinelobbies": f"{REMOTE_ROOT}/03_MemoryLobbies",
}
REMOTE_MANIFEST = f"{REMOTE_ROOT}/.spine_manifest.json"
LEGACY_MANIFEST = f"{REMOTE_CATEGORY_ROOTS['spinecharacters']}/.character_spine_manifest.json"
EXTRACT_TYPES = ["Texture2D", "TextAsset"]
SPINE_PATTERN = re.compile(r"spinecharacters|spinelobbies", re.I)
DEFAULT_WORKERS = max(1, min(6, int(os.getenv("SPINE_WORKERS", "4"))))
DOWNLOAD_WORKERS = max(1, min(8, int(os.getenv("SPINE_DOWNLOAD_WORKERS", str(DEFAULT_WORKERS)))))
UPLOAD_WORKERS = max(1, min(6, int(os.getenv("SPINE_UPLOAD_WORKERS", "3"))))


def get_spine_info(filename):
    name = filename or ""
    match = re.search(r"(spinecharacters|spinelobbies)-([^-]+)-", name, re.I)
    if not match:
        match = re.search(r"(spinecharacters|spinelobbies)[_-]([^-_]+)[_-]", name, re.I)
    if not match:
        return None
    role = match.group(2).strip("_-")
    return {"category": match.group(1).lower(), "role": role, "common": role.lower() in {"mxcommon", "common"}}


def collect_spine_packs(catalog, category=None):
    result = {}
    seen = set()
    for pack in catalog.get("FullPatchPacks", []) + catalog.get("UpdatePacks", []):
        pack_name = pack.get("PackName")
        if not pack_name or pack_name in seen:
            continue
        seen.add(pack_name)
        files = [item for item in pack.get("BundleFiles", []) if SPINE_PATTERN.search(item.get("Name", ""))]
        if category:
            files = [item for item in files if (get_spine_info(item.get("Name", "")) or {}).get("category") == category]
        if files:
            result[pack_name] = {"PackName": pack_name, "BundleFiles": files}
        else:
            print(f"[跳过] ZIP 不含目标 Spine Bundle: {pack_name}")
    return result


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class SpinePublisher:
    def __init__(self, category):
        if category not in REMOTE_CATEGORY_ROOTS:
            raise ValueError(f"不支持的 Spine 类型: {category}")
        self.server = "JP"
        self.category = category
        self.downloader = ResourceDownloader("JP", verbose=True)
        self.extractor = None
        self.ssh = SSHServer(host=os.environ["SERVER_HOST"], username="root", password=os.environ["SERVER_PASSWORD"], port=22)
        self.manifest = {"version": 2, "bundles": {}, "roles": {}}
        self.log_lock = threading.Lock()

    def log(self, message):
        with self.log_lock:
            print(message, flush=True)

    def load_manifest(self):
        try:
            manifest_path = REMOTE_MANIFEST if self.ssh.exists(REMOTE_MANIFEST) else LEGACY_MANIFEST
            raw = self.ssh.read_file(manifest_path)
            data = json.loads(raw)
            if isinstance(data, dict) and isinstance(data.get("bundles"), dict):
                self.manifest = data
                self.log(f"[记录] 已从服务器读取 {len(data['bundles'])} 条 Bundle 记录")
                return
        except Exception as exc:
            self.log(f"[记录] 服务器记录不存在或读取失败，按首次运行处理: {exc}")
        self.manifest = {"version": 2, "bundles": {}, "roles": {}}

    @staticmethod
    def infer_role_from_asset(filename):
        # 保留完整资源名，例如 Yoshimi_spr.png 对应 yoshimi_spr 目录。
        return os.path.splitext(os.path.basename(filename))[0].strip().lower()

    @staticmethod
    def _role_records(local_root):
        entries = []
        for root, _, files in os.walk(local_root):
            for filename in files:
                path = os.path.join(root, filename)
                entries.append({"name": os.path.relpath(path, local_root).replace("\\", "/"), "size": os.path.getsize(path), "sha256": file_sha256(path)})
        return entries

    def extract_one(self, bundle_path, output_dir):
        os.makedirs(output_dir, exist_ok=True)
        if self.extractor is None:
            raise RuntimeError("BundleExtractor 尚未初始化")
        self.extractor.extract_bundle(bundle_path, extract_types=EXTRACT_TYPES, extract_root=output_dir, use_type_subdir=False, auto_rename_suffix=["Texture2D"], strict=True)
        return [os.path.join(output_dir, name) for name in os.listdir(output_dir) if os.path.isfile(os.path.join(output_dir, name))]

    def download_and_unpack(self, packs, required_names, work_root):
        pack_names = sorted({pack for pack, bundle in required_names})
        zip_root = os.path.join(work_root, "zips")
        os.makedirs(zip_root, exist_ok=True)
        self.log(f"[下载] 并发下载 {len(pack_names)} 个 ZIP，线程数={DOWNLOAD_WORKERS}")
        self.downloader.get_bundle_files(pack_names, save_path=zip_root, workers=DOWNLOAD_WORKERS)
        pack_dirs = {}
        def unpack(pack_name):
            zip_path = os.path.join(zip_root, pack_name)
            if not os.path.isfile(zip_path):
                raise FileNotFoundError(f"ZIP 下载失败: {zip_path}")
            extract_dir = os.path.join(work_root, "packs", hashlib.sha1(pack_name.encode()).hexdigest()[:12])
            os.makedirs(extract_dir, exist_ok=True)
            ZipUtils.extract_zip(zip_path, extract_dir)
            return pack_name, extract_dir
        with ThreadPoolExecutor(max_workers=min(DOWNLOAD_WORKERS, max(1, len(pack_names)))) as pool:
            futures = [pool.submit(unpack, name) for name in pack_names]
            for future in as_completed(futures):
                pack_name, path = future.result()
                pack_dirs[pack_name] = path
                self.log(f"[解压 ZIP] {pack_name}")
        bundle_paths = {}
        for pack_name, bundle_name in required_names:
            bundle_path = os.path.join(pack_dirs[pack_name], bundle_name)
            if not os.path.isfile(bundle_path):
                raise FileNotFoundError(f"ZIP 内缺少 Bundle: {pack_name} / {bundle_name}")
            bundle_paths[(pack_name, bundle_name)] = bundle_path
        return bundle_paths

    def upload_role_atomic(self, local_root, role):
        file_count = sum(len(files) for _, _, files in os.walk(local_root))
        remote_parent = REMOTE_CATEGORY_ROOTS[self.category]
        remote_role = f"{remote_parent}/{role}"
        if not file_count:
            self.log(f"[跳过] 没有可上传资源: {self.category}/{role}")
            return False
        token = uuid.uuid4().hex[:10]
        stage_name = f".{role}.upload-{token}"
        local_stage = os.path.join(os.path.dirname(local_root), stage_name)
        if os.path.exists(local_stage):
            shutil.rmtree(local_stage)
        shutil.copytree(local_root, local_stage)
        remote_stage = f"{remote_parent}/{stage_name}"
        remote_backup = f"{remote_parent}/.{role}.backup-{token}"
        try:
            self.ssh.mkdir(remote_parent, parents=True)
            self.log(f"[上传] {self.category}/{role} ({file_count} 个文件)")
            # 上传到临时目录，完成后再原子切换，避免服务器出现半成品角色目录。
            self.ssh.upload(local_stage, remote_parent, recursive=True, create_parent=True)
            script = (
                f"set -e; "
                f"if [ -e {shlex.quote(remote_role)} ]; then mv {shlex.quote(remote_role)} {shlex.quote(remote_backup)}; fi; "
                f"mv {shlex.quote(remote_stage)} {shlex.quote(remote_role)}; "
                f"rm -rf {shlex.quote(remote_backup)}"
            )
            self.ssh.execute(script)
            return True
        except Exception:
            # 尝试恢复备份，保留服务器上次成功版本。
            rollback = (
                f"if [ -e {shlex.quote(remote_backup)} ] && [ ! -e {shlex.quote(remote_role)} ]; then mv {shlex.quote(remote_backup)} {shlex.quote(remote_role)}; fi; "
                f"rm -rf {shlex.quote(remote_stage)}"
            )
            try:
                self.ssh.execute(rollback)
            except Exception:
                pass
            raise
        finally:
            shutil.rmtree(local_stage, ignore_errors=True)

    def delete_remote_role(self, role):
        remote = f"{REMOTE_CATEGORY_ROOTS[self.category]}/{role}"
        self.log(f"[删除] {remote}")
        self.ssh.remove(remote, recursive=True, force=True)

    def save_manifest_category(self, current_bundles, current_roles, role_records, removed_roles):
        lock_dir = f"{REMOTE_ROOT}/.spine_manifest.lock"
        self.ssh.mkdir(REMOTE_ROOT, parents=True)
        acquired = False
        deadline = time.time() + 300
        while time.time() < deadline:
            result = self.ssh.execute(f"if mkdir {shlex.quote(lock_dir)} 2>/dev/null; then printf LOCKED; else printf WAIT; fi", timeout=30)
            if result.strip() == "LOCKED":
                acquired = True
                break
            time.sleep(2)
        if not acquired:
            raise TimeoutError("等待 Spine manifest 文件锁超时")
        local_manifest = None
        remote_tmp = f"{REMOTE_MANIFEST}.tmp-{uuid.uuid4().hex}"
        try:
            data = {"version": 2, "bundles": {}, "roles": {}}
            if self.ssh.exists(REMOTE_MANIFEST):
                try:
                    data = json.loads(self.ssh.read_file(REMOTE_MANIFEST))
                except Exception:
                    self.log("[警告] 服务器 manifest JSON 无法解析，将重建受影响分类记录")
            elif self.ssh.exists(LEGACY_MANIFEST):
                try:
                    data = json.loads(self.ssh.read_file(LEGACY_MANIFEST))
                except Exception:
                    pass
            data.setdefault("bundles", {})
            data.setdefault("roles", {})
            old_bundles = data["bundles"]
            data["bundles"] = {name: record for name, record in old_bundles.items() if record.get("category") != self.category}
            data["bundles"].update(current_bundles)
            for role in current_roles:
                if role in role_records:
                    data["roles"][f"{self.category}/{role}"] = role_records[role]
            for role in removed_roles:
                data["roles"].pop(f"{self.category}/{role}", None)
            data["version"] = 2
            local_manifest = tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False)
            json.dump(data, local_manifest, ensure_ascii=False, indent=2)
            local_manifest.close()
            self.ssh.upload_file(local_manifest.name, remote_tmp, create_parent=True)
            self.ssh.execute(f"mv {shlex.quote(remote_tmp)} {shlex.quote(REMOTE_MANIFEST)}")
            self.manifest = data
            self.log(f"[记录] {self.category} 分类记录已原子更新")
        finally:
            if local_manifest:
                try:
                    os.unlink(local_manifest.name)
                except OSError:
                    pass
            self.ssh.execute(f"rmdir {shlex.quote(lock_dir)} 2>/dev/null || true", timeout=30)

    def run(self):
        work_root = tempfile.mkdtemp(prefix=f"jp_spine_{self.category}_")
        try:
            self.load_manifest()
            catalog = self.downloader.get_bundle_packing()
            packs = collect_spine_packs(catalog, self.category)
            if not packs:
                self.log(f"[任务] {self.category} 当前没有 Spine ZIP，检查并清理服务器上的过期记录")
            current_bundles = {}
            bundle_info = {}
            for pack_name, pack in packs.items():
                for bundle in pack["BundleFiles"]:
                    name = bundle.get("Name", "")
                    info = get_spine_info(name)
                    if not info:
                        continue
                    record = {"pack": pack_name, "size": bundle.get("Size", 0), "crc": bundle.get("Crc", 0), "file_hash": bundle.get("FileHash"), "signature": bundle.get("Signature"), "category": info["category"], "role": info["role"]}
                    current_bundles[name] = record
                    bundle_info[(pack_name, name)] = info
            old_bundles = self.manifest.get("bundles", {})
            changed_roles = set()
            for name, record in current_bundles.items():
                if old_bundles.get(name) != record:
                    role = record["role"].lower()
                    changed_roles.add("*" if role in {"mxcommon", "common"} else role)
            removed_bundle_names = [name for name, record in old_bundles.items() if record.get("category") == self.category and name not in current_bundles]
            for name in removed_bundle_names:
                old = old_bundles[name]
                role = str(old.get("role", "")).lower()
                changed_roles.add("*" if role in {"mxcommon", "common"} else role)
            if not changed_roles:
                self.log(f"[记录] {self.category} Bundle 无变化，跳过提取和上传")
                return
            old_role_names = {key.split("/", 1)[1] for key in self.manifest.get("roles", {}) if key.startswith(self.category + "/")}
            all_named_roles = {record["role"].lower() for record in current_bundles.values() if record["role"].lower() not in {"mxcommon", "common"}}
            if "*" in changed_roles:
                target_roles = set(all_named_roles) | old_role_names
            else:
                target_roles = {role for role in changed_roles if role != "*"}
            # Common Bundle 可能包含多个角色的资源，因此它必须先提取；之后各角色 Bundle 并行提取并按角色完成即上传。
            common_items = [(pack, name) for (pack, name), info in bundle_info.items() if info["common"]]
            role_items = {}
            for key, info in bundle_info.items():
                if not info["common"] and info["role"].lower() in target_roles:
                    role_items.setdefault(info["role"].lower(), []).append(key)
            required = common_items + [item for items in role_items.values() for item in items]
            if not required:
                for role in target_roles:
                    self.delete_remote_role(role)
                self.save_manifest_category(current_bundles, set(), {}, target_roles)
                return
            bundle_paths = self.download_and_unpack(packs, required, work_root)
            self.log(f"[任务] 分类={self.category}，变更角色={len(target_roles)}，Bundle={len(required)}，提取线程={DEFAULT_WORKERS}")
            # 工具只初始化一次，再由多个线程并发启动独立 UABEA 子进程。
            self.extractor = BundleExtractor()

            common_output = {}
            if common_items:
                common_root = os.path.join(work_root, "common")
                os.makedirs(common_root, exist_ok=True)
                with ThreadPoolExecutor(max_workers=min(DEFAULT_WORKERS, len(common_items))) as pool:
                    futures = {pool.submit(self.extract_one, bundle_paths[key], os.path.join(common_root, hashlib.sha1((key[0] + key[1]).encode()).hexdigest()[:12])): key for key in common_items}
                    for future in as_completed(futures):
                        key = futures[future]
                        files = future.result()
                        for path in files:
                            role = self.infer_role_from_asset(os.path.basename(path))
                            if role:
                                common_output.setdefault(role, []).append(path)
                        self.log(f"[提取完成] Common Bundle: {key[1]}")
            all_current_roles = set(all_named_roles) | set(common_output)
            if "*" in changed_roles:
                target_roles = all_current_roles | old_role_names
            # 删除 catalog 已不存在且 common Bundle 中也没有资源的角色。
            removed_roles = (old_role_names | {role for role in changed_roles if role != "*"}) - all_current_roles
            for role in sorted(removed_roles):
                self.delete_remote_role(role)
            target_roles &= all_current_roles
            if not target_roles:
                self.save_manifest_category(current_bundles, all_current_roles, {}, removed_roles)
                return

            role_stage_root = os.path.join(work_root, "role_stage")
            os.makedirs(role_stage_root, exist_ok=True)
            role_stages = {}
            role_pending = {}
            for role in sorted(target_roles):
                stage = os.path.join(role_stage_root, role)
                os.makedirs(stage, exist_ok=True)
                for source in sorted(common_output.get(role, [])):
                    shutil.copy2(source, os.path.join(stage, os.path.basename(source)))
                role_stages[role] = stage
                role_pending[role] = len(role_items.get(role, []))

            role_records = {}
            upload_futures = {}
            # Common-only 角色立即进入上传，不等待其他角色的 Bundle 提取。
            with ThreadPoolExecutor(max_workers=UPLOAD_WORKERS) as upload_pool:
                for role in sorted(target_roles):
                    if role_pending[role] == 0:
                        stage = role_stages[role]
                        upload_futures[upload_pool.submit(self.upload_role_atomic, stage, role)] = role
                extraction_futures = {}
                with ThreadPoolExecutor(max_workers=DEFAULT_WORKERS) as extract_pool:
                    for role, items in role_items.items():
                        for key in items:
                            out_dir = os.path.join(work_root, "role_extract", hashlib.sha1((key[0] + key[1]).encode()).hexdigest()[:12])
                            extraction_futures[extract_pool.submit(self.extract_one, bundle_paths[key], out_dir)] = (role, key)
                    completed_by_role = {role: {} for role in target_roles}
                    for future in as_completed(extraction_futures):
                        role, key = extraction_futures[future]
                        completed_by_role[role][key] = future.result()
                        role_pending[role] -= 1
                        self.log(f"[提取完成] {key[1]}，角色 {role} 剩余 Bundle={role_pending[role]}")
                        if role_pending[role] == 0:
                            stage = role_stages[role]
                            for bundle_key in sorted(completed_by_role[role], key=lambda item: (item[0], item[1])):
                                for source in completed_by_role[role][bundle_key]:
                                    destination = os.path.join(stage, os.path.basename(source))
                                    shutil.copy2(source, destination)
                            if sum(len(files) for _, _, files in os.walk(stage)):
                                upload_futures[upload_pool.submit(self.upload_role_atomic, stage, role)] = role
                            else:
                                self.log(f"[跳过] 角色无可上传资源: {role}")
                for future in as_completed(upload_futures):
                    role = upload_futures[future]
                    if future.result():
                        role_records[role] = self._role_records(role_stages[role])
                        self.log(f"[上传完成] {self.category}/{role}")
                    else:
                        self.log(f"[上传跳过] {self.category}/{role}")

            failed_roles = target_roles - set(role_records)
            if failed_roles:
                raise RuntimeError(f"以下角色未能成功上传，manifest 不更新: {', '.join(sorted(failed_roles))}")
            # 只有所有目标角色上传成功后才提交该分类的增量记录。
            current_roles_for_manifest = (old_role_names - removed_roles) | all_current_roles
            self.save_manifest_category(current_bundles, current_roles_for_manifest, role_records, removed_roles)
        finally:
            self.ssh.close()
            shutil.rmtree(work_root, ignore_errors=True)


if __name__ == "__main__":
    category = sys.argv[1] if len(sys.argv) > 1 else os.getenv("SPINE_CATEGORY", "spinecharacters")
    SpinePublisher(category).run()
