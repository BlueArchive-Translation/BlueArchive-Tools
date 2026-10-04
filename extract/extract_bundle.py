import json
import os
import re
import shutil
import tempfile
from concurrent.futures import ProcessPoolExecutor, as_completed

from utils.download import ResourceDownloader
from utils.util import ZipUtils
from utils.git import Git
from utils.config import Config
from utils.server import SSHServer
from xtractor.bundle import BundleExtractor


_SPECIAL_BUNDLE_PATTERN = re.compile(r"(spinecharacters|spinelobbies)", re.I)
_SPECIAL_LOGICAL_NAME_PATTERN = re.compile(r"^(.*?)-_mxdependency-(?:textures|textassets)-\d{4}-\d{2}-\d{2}_assets_all_\d+\.bundle$", re.I)
_ROLE_NAME_PATTERN = re.compile(r"(?:spinecharacters|spinelobbies)-([^-]+)-", re.I)


def _extract_bundle_worker(args):
    bundle_path, extract_root, extract_types = args
    try:
        BundleExtractor().extract_bundle(bundle_path, extract_types=extract_types, extract_root=extract_root, use_type_subdir=False)
        resources = []
        for root, _, files in os.walk(extract_root):
            for name in files:
                path = os.path.join(root, name)
                resources.append({"name": os.path.relpath(path, extract_root).replace("\\", "/"), "size": os.path.getsize(path)})
        return True, resources, None
    except Exception as e:
        return False, [], str(e)


class BundlePublisher:
    REMOTE_SPECIAL_ROOT = "/var/www/web"

    def __init__(self, server):
        self.server = server
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo_dir = os.path.join(self.temp_dir.name, f"BA-Bundles-Extract-{server}")
        self.config = {}
        self.git = None
        self.ssh = SSHServer(host=os.environ["SERVER_HOST"], username="root", password=os.environ["SERVER_PASSWORD"], port=22)
        self.downloader = ResourceDownloader(server, verbose=True)

    def _load_config(self):
        if os.path.isfile(Config.bundle_config):
            try:
                with open(Config.bundle_config, "r", encoding="utf-8") as f:
                    self.config = json.load(f)
            except Exception:
                self.config = {}
        else:
            self.config = {}

    def _save_config(self):
        parent = os.path.dirname(Config.bundle_config)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(Config.bundle_config, "w", encoding="utf-8") as f:
            json.dump(self.config, f, ensure_ascii=False, indent=2)

    def _clone(self):
        repo_url = Config.Bundle_repositories.format(server=self.server)
        Git().clone(repo_url, self.repo_dir)
        self.git = Git(self.repo_dir)

    def _get_logical_bundle_name(self, filename):
        match = _SPECIAL_LOGICAL_NAME_PATTERN.match(filename)
        if match:
            return match.group(1)
        return re.sub(r"_\d+\.bundle$", "", filename, flags=re.I)

    def _get_special_info(self, filename):
        match = _SPECIAL_BUNDLE_PATTERN.search(filename)
        if not match:
            return None
        role = _ROLE_NAME_PATTERN.search(filename)
        if not role:
            return None
        return match.group(1).lower(), role.group(1)

    def _is_extractable(self, filename):
        lower = filename.lower()
        return "textures" in lower or ("textassets" in lower and _SPECIAL_BUNDLE_PATTERN.search(lower))

    def _build_groups(self, bundle_files, zip_root):
        groups = {}
        for bundle in bundle_files:
            filename = bundle["Name"]
            if not self._is_extractable(filename):
                continue
            path = os.path.join(zip_root, filename)
            if not os.path.isfile(path):
                continue
            logical_name = self._get_logical_bundle_name(filename)
            group = groups.setdefault(logical_name, {"name": logical_name, "special": self._get_special_info(filename), "sources": []})
            group["sources"].append({
                "path": path,
                "name": filename,
                "size": bundle.get("Size", 0),
                "crc": bundle.get("Crc", 0),
                "type": "textassets" if "textassets" in filename.lower() else "textures",
            })
        return groups

    def _is_changed(self, old, group):
        if not old:
            return True
        old_sources = old.get("sources", {})
        if len(old_sources) != len(group["sources"]):
            return True
        for source in group["sources"]:
            item = old_sources.get(source["name"])
            if not item or item.get("size") != source["size"] or item.get("crc") != source["crc"]:
                return True
        return False

    def _merge(self, source_root, target_root):
        os.makedirs(target_root, exist_ok=True)
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

    def _upload_special(self, local_root, group):
        if not group["special"]:
            return
        category, role = group["special"]
        remote = os.path.join(self.REMOTE_SPECIAL_ROOT, category, role)
        self.ssh.remove_dir(remote)
        self.ssh.upload_directory(local_root, remote, create_parent=True)

    def _stage_bundle(self, target_root):
        path = os.path.relpath(target_root, self.repo_dir).replace("\\", "/")
        self.git.add(path)

    def _extract_group(self, group):
        target_root = os.path.join(self.repo_dir, group["name"])
        if os.path.exists(target_root):
            shutil.rmtree(target_root)
        os.makedirs(target_root, exist_ok=True)
        tasks = []
        temp_roots = []
        for source in group["sources"]:
            temp_root = tempfile.mkdtemp(prefix="bundle_extract_")
            temp_roots.append(temp_root)
            extract_types = ["TextAsset"] if source["type"] == "textassets" else ["Texture2D"]
            tasks.append((source["path"], temp_root, extract_types))
        resources = []
        try:
            with ProcessPoolExecutor(max_workers=os.cpu_count() or 1) as executor:
                futures = {executor.submit(_extract_bundle_worker, task): (source, temp_root) for task, source, temp_root in zip(tasks, group["sources"], temp_roots)}
                for future in as_completed(futures):
                    source, temp_root = futures[future]
                    success, result, error = future.result()
                    if not success:
                        raise RuntimeError(f"extract failed: {source['name']}: {error}")
                    self._merge(temp_root, target_root)
                    resources.extend(result)
        finally:
            for temp_root in temp_roots:
                shutil.rmtree(temp_root, ignore_errors=True)
        self.config[group["name"]] = {
            "sources": {source["name"]: {"size": source["size"], "crc": source["crc"]} for source in group["sources"]},
            "resources": resources,
        }
        self._stage_bundle(target_root)
        self._upload_special(target_root, group)

    def _commit_zip(self, pack_name):
        if not self.git.has_staged_changes():
            return
        self.git.commit(f"Update bundles: {pack_name}")
        self.git.push()

    def _process_zip(self, zip_path, pack):
        zip_root = tempfile.mkdtemp(prefix="bundle_zip_")
        try:
            ZipUtils.extract_zip(zip_path, zip_root)
            groups = self._build_groups(pack.get("BundleFiles", []), zip_root)
            changed = False
            for group in groups.values():
                if not self._is_changed(self.config.get(group["name"]), group):
                    continue
                self._extract_group(group)
                changed = True
            self._save_config()
            if changed:
                self._commit_zip(pack.get("PackName", "unknown"))
        finally:
            shutil.rmtree(zip_root, ignore_errors=True)

    def _download_pack(self, pack):
        filename = pack["PackName"]
        temp_dir = tempfile.mkdtemp(prefix="patch_pack_")
        try:
            result = self.downloader.get_bundle_files([filename], save_path=temp_dir)
            path = os.path.join(temp_dir, os.path.basename(filename))
            if isinstance(result, dict):
                path = result.get(filename) or result.get(os.path.basename(filename)) or path
            elif isinstance(result, list) and result:
                path = result[0]
            elif isinstance(result, str):
                path = result
            if not os.path.isfile(path):
                raise FileNotFoundError(f"Pack download failed: {filename}")
            return path, temp_dir
        except Exception:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise

    def _process_packs(self, catalog):
        packs = catalog.get("FullPatchPacks", []) + catalog.get("UpdatePacks", [])
        seen = set()
        for pack in packs:
            name = pack.get("PackName")
            if not name or name in seen:
                continue
            seen.add(name)
            zip_path, temp_dir = self._download_pack(pack)
            try:
                self._process_zip(zip_path, pack)
            finally:
                shutil.rmtree(temp_dir, ignore_errors=True)

    def _commit_config(self):
        self.git.add(Config.bundle_config)
        if self.git.has_staged_changes():
            self.git.commit("Update bundle config")
            self.git.push()

    def _finalize(self):
        self._save_config()
        self._commit_config()

    def run(self):
        try:
            self._load_config()
            self._clone()
            catalog = self.downloader.get_bundle_packing()
            self._process_packs(catalog)
            self._finalize()
        finally:
            self.ssh.close()
            self.temp_dir.cleanup()
