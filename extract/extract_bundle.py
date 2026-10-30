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
from xtractor.bundle import BundleExtractor


def _logical_name(filename):
    match = re.match(r"^(.*?)-textures-.*?\.bundle$", filename, re.I)
    return match.group(1) if match else re.sub(r"_\d+\.bundle$", "", filename, flags=re.I)


def _build_groups(bundle_files, zip_root):
    groups = {}
    for bundle in bundle_files:
        filename = bundle.get("Name", "")
        if re.search(r"(spinecharacters|spinelobbies)", filename, re.I):
            print(f"[跳过] Spine Bundle 由独立流程处理: {filename}")
            continue
        if "textures" not in filename.lower():
            continue
        path = os.path.join(zip_root, filename)
        if not os.path.isfile(path):
            print(f"[跳过] ZIP 中不存在 Bundle: {filename}")
            continue
        logical_name = _logical_name(filename)
        group = groups.setdefault(logical_name, {"name": logical_name, "sources": []})
        group["sources"].append({
            "path": path,
            "name": filename,
            "size": bundle.get("Size", 0),
            "crc": bundle.get("Crc", 0),
            "extract_type": "Texture2D",
        })
    return groups


def _extract_zip_worker(args):
    zip_path, pack, zip_name = args
    zip_root = tempfile.mkdtemp(prefix="bundle_zip_")
    result_root = tempfile.mkdtemp(prefix="bundle_result_")
    extractor = BundleExtractor()
    results = []

    try:
        print(f"[ZIP] 开始处理: {zip_name}")
        ZipUtils.extract_zip(zip_path, zip_root)
        groups = _build_groups(pack.get("BundleFiles", []), zip_root)
        print(
            f"[扫描] {zip_name} 找到 {len(groups)} 个 Bundle 分组，"
            f"共 {sum(len(group['sources']) for group in groups.values())} 个 Bundle"
        )

        for group in groups.values():
            target_root = os.path.join(result_root, group["name"])
            os.makedirs(target_root, exist_ok=True)
            resources = []

            for source in group["sources"]:
                temp_root = tempfile.mkdtemp(prefix="bundle_extract_")
                try:
                    print(f"[提取] {zip_name}: {source['name']}")
                    extractor.extract_bundle(
                        source["path"],
                        extract_types=[source["extract_type"]],
                        extract_root=temp_root,
                        use_type_subdir=False,
                        auto_rename_suffix=["Texture2D"],
                    )

                    for root, _, files in os.walk(temp_root):
                        relative = os.path.relpath(root, temp_root)
                        target = (
                            target_root
                            if relative == "."
                            else os.path.join(target_root, relative)
                        )
                        os.makedirs(target, exist_ok=True)

                        for name in files:
                            src = os.path.join(root, name)
                            dst = os.path.join(target, name)

                            if os.path.exists(dst):
                                os.remove(dst)

                            shutil.move(src, dst)
                            resources.append(
                                {
                                    "name": os.path.relpath(src, temp_root).replace("\\", "/"),
                                    "size": os.path.getsize(dst),
                                }
                            )
                finally:
                    shutil.rmtree(temp_root, ignore_errors=True)

            results.append(
                {
                    "name": group["name"],
                    "sources": {
                        source["name"]: {
                            "size": source["size"],
                            "crc": source["crc"],
                        }
                        for source in group["sources"]
                    },
                    "resources": resources,
                    "path": target_root,
                }
            )

        print(f"[ZIP] 处理完成: {zip_name}")

        return {
            "zip_name": zip_name,
            "result_root": result_root,
            "groups": results,
            "bundle_count": sum(len(group["sources"]) for group in groups.values()),
        }

    except Exception:
        shutil.rmtree(result_root, ignore_errors=True)
        raise

    finally:
        shutil.rmtree(zip_root, ignore_errors=True)
        shutil.rmtree(os.path.dirname(zip_path), ignore_errors=True)


class BundlePublisher:
    def __init__(self, server):
        self.server = server
        self.repo_dir = f"BA-Bundles-Extract-{server}"
        self.config = {}
        self.repo_git = Git(self.repo_dir)
        self.git = Git()
        self.downloader = ResourceDownloader(server, verbose=True)
        self.bundle_extractor = BundleExtractor()
        self.matrix_index = int(os.environ.get("MATRIX_INDEX", "0"))
        self.matrix_total = int(os.environ.get("MATRIX_TOTAL", "1"))

    def _logical_name(self, filename):
        return _logical_name(filename)

    def _build_groups(self, bundle_files, zip_root):
        return _build_groups(bundle_files, zip_root)

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

    def _apply_result(self, result):
        zip_name = result["zip_name"]
        changed_count = 0

        for group in result["groups"]:
            current = {
                "sources": group["sources"],
                "resources": group["resources"],
            }

            if not self._is_changed(
                {
                    "name": group["name"],
                    "sources": [
                        {
                            "name": name,
                            "size": value["size"],
                            "crc": value["crc"],
                        }
                        for name, value in group["sources"].items()
                    ],
                }
            ):
                print(f"[跳过] 无变化: {group['name']}")
                continue

            target_root = os.path.join(self.repo_dir, zip_name, group["name"])

            print(f"[变化] {group['name']}")

            self._merge(group["path"], target_root)
            self.config[group["name"]] = current
            changed_count += 1

        shutil.rmtree(result["result_root"], ignore_errors=True)

        print(
            f"[ZIP] 应用完成: {zip_name}，"
            f"变化分组 {changed_count} 个，"
            f"处理 Bundle {result['bundle_count']} 个"
        )

        return result["bundle_count"]

    def _commit_resources(self, bundle_count=0):
        self.repo_git.pull()
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
        zip_name = os.path.splitext(os.path.basename(zip_path))[0]
        result = _extract_zip_worker((zip_path, pack, zip_name))
        return self._apply_result(result)

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

    def _process_pack_worker(self, pack):
        filename = pack["PackName"]
        temp_dir = tempfile.mkdtemp(prefix="patch_pack_")

        try:
            print(f"[下载] {filename}")

            self.downloader.get_bundle_files(
                [filename],
                save_path=temp_dir,
            )

            path = os.path.join(temp_dir, filename)

            if not os.path.isfile(path):
                raise FileNotFoundError(f"Pack download failed: {path}")

            print(f"[下载] 完成: {filename}")
            zip_name = os.path.splitext(filename)[0]
            return _extract_zip_worker((path, pack, zip_name))

        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def _process_packs(self, catalog):
        packs = catalog.get("FullPatchPacks", []) + catalog.get("UpdatePacks", [])
        seen = set()
        unique_packs = []

        for pack in packs:
            name = pack.get("PackName")

            if not name or name in seen:
                continue

            seen.add(name)
            bundle_files = pack.get("BundleFiles", [])
            has_regular_texture = any(
                "textures" in item.get("Name", "").lower()
                and not re.search(
                    r"(spinecharacters|spinelobbies)", item.get("Name", ""), re.I
                )
                for item in bundle_files
            )
            if has_regular_texture:
                unique_packs.append(pack)
            else:
                print(f"[跳过] ZIP 不含普通 Texture Bundle: {name}")

        packs = [
            pack
            for index, pack in enumerate(unique_packs)
            if index % self.matrix_total == self.matrix_index
        ]

        print(
            f"[任务] 总 Pack {len(unique_packs)} 个，"
            f"矩阵 [{self.matrix_index + 1}/{self.matrix_total}] "
            f"处理 {len(packs)} 个"
        )

        if not packs:
            return

        workers = min(
            max(1, int(os.environ.get("BUNDLE_WORKERS", "4"))),
            len(packs),
        )

        with ProcessPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(
                    self._process_pack_worker,
                    pack,
                ): pack
                for pack in packs
            }

            for future in as_completed(futures):
                pack = futures[future]
                name = pack.get("PackName")

                try:
                    result = future.result()
                    self._apply_result(result)

                except Exception as e:
                    print(f"[错误] Pack 处理失败: {name}: {e}")
                    raise

        self._commit_resources()

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

            with open(Config.bundle_config, "w", encoding="utf-8") as f:
                json.dump(self.config, f, ensure_ascii=False, indent=2)

            print(
                f"[配置] 矩阵 [{self.matrix_index + 1}/{self.matrix_total}] "
                f"Bundle 配置已生成"
            )
        finally:
            print("[结束] 清理临时资源")
            shutil.rmtree(self.repo_dir, ignore_errors=True)
