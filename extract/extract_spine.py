import hashlib
import json
import os
import re
import shutil
import tempfile

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


def get_spine_info(filename):
    name = filename or ""
    match = re.search(r"(spinecharacters|spinelobbies)-([^-]+)-", name, re.I)
    if not match:
        # Some catalog names separate the marker and role with an underscore.
        match = re.search(r"(spinecharacters|spinelobbies)[_-]([^-_]+)[_-]", name, re.I)
    if not match:
        return None
    role = match.group(2).strip("_-")
    return {
        "category": match.group(1).lower(),
        "role": role,
        "common": role.lower() in {"mxcommon", "common"},
    }


def collect_spine_packs(catalog):
    result = {}
    seen = set()
    for pack in catalog.get("FullPatchPacks", []) + catalog.get("UpdatePacks", []):
        pack_name = pack.get("PackName")
        if not pack_name or pack_name in seen:
            continue
        seen.add(pack_name)
        files = [
            item
            for item in pack.get("BundleFiles", [])
            if SPINE_PATTERN.search(item.get("Name", ""))
        ]
        if files:
            result[pack_name] = {"PackName": pack_name, "BundleFiles": files}
        else:
            print(f"[跳过] ZIP 不含 Spine Bundle: {pack_name}")
    return result


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class SpinePublisher:
    def __init__(self):
        self.server = "JP"
        self.downloader = ResourceDownloader("JP", verbose=True)
        self.extractor = BundleExtractor()
        self.ssh = SSHServer(
            host=os.environ["SERVER_HOST"],
            username="root",
            password=os.environ["SERVER_PASSWORD"],
            port=22,
        )
        self.manifest = {"version": 1, "bundles": {}, "roles": {}}

    def load_manifest(self):
        try:
            manifest_path = REMOTE_MANIFEST if self.ssh.exists(REMOTE_MANIFEST) else LEGACY_MANIFEST
            raw = self.ssh.read_file(manifest_path)
            data = json.loads(raw)
            if isinstance(data, dict) and isinstance(data.get("bundles"), dict):
                self.manifest = data
                print(f"[记录] 已从服务器读取 {len(data['bundles'])} 条 Bundle 记录: {manifest_path}")
                return
        except Exception as exc:
            print(f"[记录] 服务器记录不存在或读取失败，按首次运行处理: {exc}")
        self.manifest = {"version": 1, "bundles": {}, "roles": {}}

    @staticmethod
    def merge_directory(source, target):
        for root, _, files in os.walk(source):
            relative = os.path.relpath(root, source)
            target_root = target if relative == "." else os.path.join(target, relative)
            os.makedirs(target_root, exist_ok=True)
            for filename in files:
                src = os.path.join(root, filename)
                dst = os.path.join(target_root, filename)
                if os.path.exists(dst):
                    os.remove(dst)
                shutil.move(src, dst)

    @staticmethod
    def infer_role_from_asset(filename):
        # Preserve the complete asset stem as the role directory name, including suffixes such as _spr.
        return os.path.splitext(os.path.basename(filename))[0].strip().lower()

    def upload_role(self, local_root, category, role):
        file_count = sum(len(files) for _, _, files in os.walk(local_root))
        if not file_count:
            print(f"[跳过] 没有可上传资源: {category}/{role}")
            return
        remote = f"{REMOTE_CATEGORY_ROOTS[category]}/{role}"
        print(f"[上传] {remote} ({file_count} 个文件)")
        self.ssh.remove(remote, recursive=True, force=True)
        self.ssh.mkdir(remote, parents=True)
        for filename in os.listdir(local_root):
            local_path = os.path.join(local_root, filename)
            self.ssh.upload(
                local_path,
                f"{remote}/{filename}",
                recursive=os.path.isdir(local_path),
                create_parent=True,
            )

    def run(self):
        work_root = tempfile.mkdtemp(prefix="jp_spine_")
        try:
            self.load_manifest()
            catalog = self.downloader.get_bundle_packing()
            packs = collect_spine_packs(catalog)
            if not packs:
                print("[任务] 没有 Spine ZIP，无需处理")
                return
            current_bundles = {}
            for pack_name, pack in packs.items():
                for bundle in pack["BundleFiles"]:
                    name = bundle.get("Name", "")
                    info = get_spine_info(name)
                    if not info:
                        continue
                    key = name
                    record = {
                        "pack": pack_name,
                        "size": bundle.get("Size", 0),
                        "crc": bundle.get("Crc", 0),
                        "file_hash": bundle.get("FileHash"),
                        "signature": bundle.get("Signature"),
                        "category": info["category"],
                        "role": info["role"],
                    }
                    current_bundles[key] = record
            old_bundles = self.manifest.get("bundles", {})
            changed_roles = set()
            for name, record in current_bundles.items():
                old = old_bundles.get(name)
                if old != record:
                    role = record["role"].lower()
                    changed_roles.add((record["category"], "*" if role in {"mxcommon", "common"} else role))
            removed = set(old_bundles) - set(current_bundles)
            for name in removed:
                old = old_bundles[name]
                role = str(old.get("role", "")).lower()
                changed_roles.add(
                    (old.get("category", "spinecharacters"), "*" if role in {"mxcommon", "common"} else role)
                )
            if not changed_roles:
                print("[记录] Bundle 无变化，跳过提取和上传")
                return
            print(
                f"[任务] {len(packs)} 个 Spine ZIP，{len(current_bundles)} 个 Bundle；"
                f"变更角色: {', '.join(f'{category}/{role}' for category, role in sorted(changed_roles))}"
            )

            # Download only ZIPs that contain Spine Bundle entries; no unrelated ZIP is downloaded.
            pack_dirs = {}
            for pack_name in packs:
                zip_dir = os.path.join(work_root, "zips", os.path.splitext(pack_name)[0])
                os.makedirs(zip_dir, exist_ok=True)
                zip_path = os.path.join(zip_dir, pack_name)
                print(f"[下载] {pack_name}")
                self.downloader.get_bundle_files([pack_name], save_path=zip_dir)
                if not os.path.isfile(zip_path):
                    raise FileNotFoundError(f"ZIP 下载失败: {zip_path}")
                extract_dir = os.path.join(work_root, "packs", os.path.splitext(pack_name)[0])
                os.makedirs(extract_dir, exist_ok=True)
                ZipUtils.extract_zip(zip_path, extract_dir)
                pack_dirs[pack_name] = extract_dir

            output_root = os.path.join(work_root, "output")
            os.makedirs(output_root, exist_ok=True)
            # 全量提取当前 Spine Bundle，确保变更角色由其所有 Bundle 完整重建。
            for pack_name, pack in packs.items():
                for bundle in pack["BundleFiles"]:
                    filename = bundle.get("Name", "")
                    info = get_spine_info(filename)
                    if not info:
                        continue
                    bundle_path = os.path.join(pack_dirs[pack_name], filename)
                    if not os.path.isfile(bundle_path):
                        raise FileNotFoundError(f"ZIP 内缺少 Bundle: {pack_name} / {filename}")
                    temp_out = tempfile.mkdtemp(prefix="extract_", dir=work_root)
                    try:
                        print(f"[提取] {filename}")
                        self.extractor.extract_bundle(
                            bundle_path,
                            extract_types=EXTRACT_TYPES,
                            extract_root=temp_out,
                            use_type_subdir=False,
                            auto_rename_suffix=["Texture2D"],
                        )
                        files = [
                            os.path.join(temp_out, filename)
                            for filename in os.listdir(temp_out)
                            if os.path.isfile(os.path.join(temp_out, filename))
                        ]
                        for src in files:
                            if info["common"]:
                                role = self.infer_role_from_asset(os.path.basename(src))
                            else:
                                role = info["role"].lower()
                            category = info["category"]
                            role_dir = os.path.join(output_root, category, role)
                            os.makedirs(role_dir, exist_ok=True)
                            dst = os.path.join(role_dir, os.path.basename(src))
                            if os.path.exists(dst):
                                os.remove(dst)
                            shutil.move(src, dst)
                    finally:
                        shutil.rmtree(temp_out, ignore_errors=True)

            all_roles = set()
            for category in REMOTE_CATEGORY_ROOTS:
                category_root = os.path.join(output_root, category)
                if os.path.isdir(category_root):
                    all_roles.update(
                        (category, role) for role in os.listdir(category_root)
                        if os.path.isdir(os.path.join(category_root, role))
                    )
            roles_to_upload = set()
            for category, role in changed_roles:
                if role == "*":
                    roles_to_upload.update(item for item in all_roles if item[0] == category)
                elif (category, role) in all_roles:
                    roles_to_upload.add((category, role))
            for category, role in sorted(roles_to_upload):
                self.upload_role(os.path.join(output_root, category, role), category, role)

            for category, role in sorted(changed_roles):
                if role != "*" and (category, role) not in all_roles:
                    remote = f"{REMOTE_CATEGORY_ROOTS[category]}/{role}"
                    print(f"[删除] 角色提取结果为空，清理旧资源: {remote}")
                    self.ssh.remove(remote, recursive=True, force=True)

            old_roles = set(self.manifest.get("roles", {}))
            current_roles = {f"{category}/{role}" for category, role in all_roles}
            for old_key in sorted(old_roles - current_roles):
                if "/" in old_key:
                    category, role = old_key.split("/", 1)
                else:
                    category, role = "spinecharacters", old_key
                if category in REMOTE_CATEGORY_ROOTS:
                    remote = f"{REMOTE_CATEGORY_ROOTS[category]}/{role}"
                    print(f"[删除] 已从目录移除角色: {remote}")
                    self.ssh.remove(remote, recursive=True, force=True)

            new_roles = {}
            for category, role in sorted(all_roles):
                entries = []
                role_root = os.path.join(output_root, category, role)
                for root, _, files in os.walk(role_root):
                    for filename in files:
                        path = os.path.join(root, filename)
                        entries.append({
                            "name": os.path.relpath(path, role_root).replace("\\", "/"),
                            "size": os.path.getsize(path),
                            "sha256": file_sha256(path),
                        })
                new_roles[f"{category}/{role}"] = entries
            self.manifest = {"version": 1, "bundles": current_bundles, "roles": new_roles}
            content = json.dumps(self.manifest, ensure_ascii=False, indent=2)
            self.ssh.write_file(REMOTE_MANIFEST, content)
            print(f"[记录] 已更新服务器记录: {REMOTE_MANIFEST}")
        finally:
            self.ssh.close()
            shutil.rmtree(work_root, ignore_errors=True)


if __name__ == "__main__":
    SpinePublisher().run()
