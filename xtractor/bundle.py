import json
import multiprocessing
import multiprocessing.queues
import multiprocessing.synchronize
import os
import stat
import struct
import subprocess
import tempfile
import platform
import io
import shutil
from os import path
from typing import Any, List, Optional, Union, Dict

from crcmanip.crc import CRC32
from crcmanip.algorithm import apply_patch, consume
from PIL import Image
from utils.console import ProgressBar
from utils.util import ZipUtils, ToolManager, FileDownloader


def build_asset_index(extractor: "BundleExtractor", folder_path: str) -> Dict[str, List[dict]]:
    """
    一次性扫描文件夹，建立 {asset_name: [match_info, ...]} 索引。
    """
    index: Dict[str, List[dict]] = {}
    extractor.search_unity_pack(folder_path, collect_index=index, collect_only=True)
    return index


def _bundle_replace_worker(task: tuple) -> tuple:
    """
    多进程 worker：将单个资源文件替换进对应 bundle 文件并修补 CRC。
    """
    bin_path, target_filepath, match, asset_name, file_path, crc_fix = task
    try:
        ext = BundleExtractor()
        ext.bin_path = bin_path
        ext._import_file_direct(target_filepath, match, asset_name, file_path, crc_fix)
        return target_filepath, True, ""
    except Exception as e:
        return target_filepath, False, f"{type(e).__name__}: {e}"


class BundleExtractor(ToolManager):
    MAIN_EXTRACT_TYPES = [
        "Texture2D", "Sprite", "AudioClip", "Font", "TextAsset",
        "Mesh", "VideoClip", "MonoBehaviour", "Shader",
    ]

    _EXPORT_FORMAT = {
        "Texture2D": "png",
        "Sprite": "png",
        "AudioClip": "wav",
        "TextAsset": "txt",
        "Font": "raw",
        "VideoClip": "raw",
        "Mesh": "raw",
        "MonoBehaviour": "json",
        "Shader": "raw",
    }

    def __init__(self, install_dir: str = "tools") -> None:
        super().__init__(install_dir)
        self.bin_path = self.ensure_tool()
        self.install_dir = install_dir

    @staticmethod
    def _print_error(message: str, exc: Optional[Exception] = None) -> None:
        if exc is None:
            print(f"[BundleExtractor][ERROR] {message}")
        else:
            print(f"[BundleExtractor][ERROR] {message}: {type(exc).__name__}: {exc}")

    @staticmethod
    def _print_warning(message: str) -> None:
        print(f"[BundleExtractor][WARNING] {message}")

    @staticmethod
    def _print_info(message: str) -> None:
        print(f"[BundleExtractor] {message}")

    def _run_uabea(self, args: List[str]) -> subprocess.CompletedProcess:
        """
        执行 UABEAvalonia CLI。
        路径参数自动转换为绝对路径，并将 cwd 设置到 UABEA 所在目录。
        """
        abs_args: List[str] = ["uabea"]
        path_flags = {"-f", "-d", "-o", "-i", "-b", "-a"}
        i = 0
        while i < len(args):
            arg = args[i]
            abs_args.append(arg)
            if arg in path_flags and i + 1 < len(args):
                i += 1
                abs_args.append(os.path.abspath(args[i]))
            i += 1
        cmd = [self.bin_path] + abs_args
        cwd = os.path.dirname(os.path.abspath(self.bin_path))
        try:
            return subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
        except Exception as e:
            self._print_error(f"执行 UABEA 失败，命令: {' '.join(cmd)}", e)
            return subprocess.CompletedProcess(cmd, returncode=-1, stdout="", stderr=str(e))

    @staticmethod
    def _parse_list_output(output: str) -> List[dict]:
        """解析 UABEA list stdout。"""
        assets: List[dict] = []
        current_source = ""
        is_bundle = False
        for line in output.splitlines():
            s = line.strip()
            if s.startswith("Bundle:"):
                current_source = s.split(":", 1)[1].strip()
                is_bundle = True
                continue
            if s.startswith("Assets File:"):
                current_source = s.split(":", 1)[1].split("(")[0].strip()
                is_bundle = False
                continue
            if not s or s.startswith("-") or s.startswith("PathID") or s.startswith("Total"):
                continue
            cols = s.split()
            try:
                int(cols[0])
            except (ValueError, IndexError):
                continue
            if is_bundle and len(cols) >= 5:
                assets.append({
                    "path_id": cols[0],
                    "entry": cols[1],
                    "type": cols[2],
                    "size": cols[3],
                    "name": " ".join(cols[4:]),
                    "source_path": current_source,
                })
            elif not is_bundle and len(cols) >= 4:
                entry_name = os.path.basename(current_source) if current_source else ""
                assets.append({
                    "path_id": cols[0],
                    "entry": entry_name,
                    "type": cols[1],
                    "size": cols[2],
                    "name": " ".join(cols[3:]),
                    "source_path": current_source,
                })
        return assets

    class _MockReadData:
        """模拟 UnityPy obj.read() 的返回对象。"""
        def __init__(self, asset_info: dict, raw_bytes: Optional[bytes] = None) -> None:
            self._info = asset_info
            self.m_Name: str = asset_info.get("name", "")
            self._raw = raw_bytes
            if raw_bytes and asset_info.get("type") == "TextAsset":
                self.m_Script = self._parse_textasset_raw(raw_bytes)
            else:
                try:
                    self.m_Script = raw_bytes.decode("utf-8") if raw_bytes else ""
                except UnicodeDecodeError:
                    self.m_Script = raw_bytes if raw_bytes else b""
            self.bundleVersion: str = ""

        @staticmethod
        def _parse_textasset_raw(raw: bytes) -> Union[str, bytes]:
            try:
                if len(raw) < 4:
                    return raw
                name_len = struct.unpack_from("<i", raw, 0)[0]
                if not (0 <= name_len <= 1024) or 4 + name_len > len(raw):
                    return raw
                offset = (4 + name_len + 3) & ~3
                if offset + 4 > len(raw):
                    return raw
                script_len = struct.unpack_from("<i", raw, offset)[0]
                offset += 4
                if not (0 <= script_len <= len(raw) - offset):
                    return raw
                script_bytes = raw[offset:offset + script_len]
                try:
                    return script_bytes.decode("utf-8")
                except UnicodeDecodeError:
                    return script_bytes
            except Exception:
                return raw

    class _MockObj:
        """模拟 UnityPy 对象。"""
        class _Type:
            def __init__(self, name: str) -> None:
                self.name = name

        def __init__(self, asset_info: dict, read_data: "BundleExtractor._MockReadData") -> None:
            self._info = asset_info
            self._read_data = read_data
            self.source_path = asset_info.get("source_path", "")
            self.path_id = asset_info.get("path_id", "")
            self.type = BundleExtractor._MockObj._Type(asset_info.get("type", ""))

        def read(self) -> "BundleExtractor._MockReadData":
            return self._read_data

    def search_unity_pack(
        self,
        pack_path: str,
        data_type: Optional[List[str]] = None,
        data_name: Optional[List[str]] = None,
        condition_connect: bool = False,
        read_obj_anyway: bool = False,
        _workers: int = 8,
        collect_index: Optional[Dict[str, List[dict]]] = None,
        collect_only: bool = False,
    ) -> List[Any]:
        """逐文件扫描 bundle 资源，可同时收集全量资源索引。"""
        def process_file(file_path: str) -> List[Any]:
            result_list = []
            if not os.path.isfile(file_path):
                return result_list
            try:
                if os.path.getsize(file_path) < 20:
                    return result_list
            except Exception as e:
                self._print_warning(f"无法读取文件大小: {file_path}，{e}")
                return result_list
            result = self._run_uabea(["list", "-f", file_path])
            if result.returncode != 0:
                self._print_error(
                    f"UABEA list 失败: {file_path}\n"
                    f"stdout: {result.stdout.strip()}\n"
                    f"stderr: {result.stderr.strip()}"
                )
                return result_list
            candidates = self._parse_list_output(result.stdout)
            if collect_index is not None:
                for info in candidates:
                    info.setdefault("source_path", file_path)
                    collect_index.setdefault(info["name"], []).append(info)
            for info in candidates:
                info.setdefault("source_path", file_path)
                if collect_only:
                    continue
                type_passed = not data_type or info["type"] in data_type
                name_passed = not data_name or info["name"] in data_name
                if condition_connect or read_obj_anyway:
                    if not (type_passed and name_passed):
                        continue
                elif not type_passed:
                    continue
                raw_bytes = None
                if info["type"] == "TextAsset":
                    source_file = info.get("source_path", "")
                    if source_file:
                        try:
                            with tempfile.TemporaryDirectory() as tmp_dir:
                                exp = self._run_uabea([
                                    "export", "-f", source_file, "-p", info["path_id"],
                                    "-o", tmp_dir, "--format", "raw",
                                ])
                                if exp.returncode == 0:
                                    for fname in os.listdir(tmp_dir):
                                        fpath = os.path.join(tmp_dir, fname)
                                        if os.path.isfile(fpath):
                                            with open(fpath, "rb") as fh:
                                                raw_bytes = fh.read()
                                            break
                                else:
                                    self._print_warning(
                                        f"TextAsset 导出失败: {source_file} PathID={info['path_id']}\n"
                                        f"stderr: {exp.stderr.strip()}"
                                    )
                        except Exception as e:
                            self._print_error(f"读取 TextAsset 失败: {source_file}", e)
                    result_list.append(BundleExtractor._MockObj(
                        info, BundleExtractor._MockReadData(info, raw_bytes)
                    ))
                else:
                    result_list.append(BundleExtractor._MockObj(
                        info, BundleExtractor._MockReadData(info)
                    ))
            return result_list

        if os.path.isdir(pack_path):
            data_list = []
            files = []
            for root, _, filenames in os.walk(pack_path):
                for filename in filenames:
                    if filename.endswith(".resS"):
                        continue
                    files.append(os.path.join(root, filename))
            self._print_info(f"扫描文件数量: {len(files)}")
            for file_path in files:
                result = process_file(file_path)
                if result:
                    self._print_info(f"找到资源: {file_path} 数量:{len(result)}")
                    data_list.extend(result)
            return data_list
        return process_file(pack_path)

    def _patch_crc(self, filepath: str, original_crc_int: int) -> bool:
        """
        在文件末尾追加 CRC patch，使最终 CRC 恢复为原始 CRC。
        """
        try:
            codec_before = CRC32()
            with open(filepath, "rb") as f:
                consume(codec_before, f)
            current_crc = codec_before.digest()
            self._print_info(
                f"CRC 修补: {os.path.basename(filepath)} | "
                f"Expected=0x{original_crc_int:08X} Current=0x{current_crc:08X}"
            )
            if current_crc == original_crc_int:
                self._print_info("文件 CRC 未发生变化，无需 Patch")
                return True
            with open(filepath, "rb") as f:
                data = f.read()
            input_io = io.BytesIO(data)
            output_io = io.BytesIO()
            apply_patch(
                crc=CRC32(),
                target_checksum=original_crc_int,
                input_handle=input_io,
                output_handle=output_io,
                target_pos=len(data),
                overwrite=False,
            )
            patched_data = output_io.getvalue()
            with open(filepath, "wb") as f:
                f.write(patched_data)
            codec_after = CRC32()
            with open(filepath, "rb") as f:
                consume(codec_after, f)
            final_crc = codec_after.digest()
            if final_crc != original_crc_int:
                self._print_error(
                    f"CRC Patch 失败: {filepath} "
                    f"Expected=0x{original_crc_int:08X} Final=0x{final_crc:08X}"
                )
                return False
            self._print_info(f"CRC Patch 成功: 0x{final_crc:08X}")
            return True
        except Exception as e:
            self._print_error(f"CRC 修补失败: {filepath}", e)
            return False

    @staticmethod
    def _new_data_to_file(
        obj_type: str,
        new_data: Any,
        asset_name: str,
        entry: str,
        path_id: str,
        tmp_dir: str,
    ) -> Optional[str]:
        stem = f"{asset_name}-{entry}-{path_id}" if entry else f"{asset_name}-{path_id}"
        if obj_type == "TextAsset":
            fpath = path.join(tmp_dir, stem + ".txt")
            raw = new_data.encode("utf-8", "surrogateescape") if isinstance(new_data, str) else bytes(new_data)
            with open(fpath, "wb") as f:
                f.write(raw)
            return fpath
        if obj_type == "Texture2D":
            if isinstance(new_data, Image.Image):
                fpath = path.join(tmp_dir, stem + ".png")
                new_data.save(fpath)
                return fpath
            if isinstance(new_data, (bytes, list)):
                fpath = path.join(tmp_dir, stem + ".dat")
                with open(fpath, "wb") as f:
                    f.write(bytes(new_data) if isinstance(new_data, list) else new_data)
                return fpath
        if obj_type == "Font" and isinstance(new_data, (bytes, list)):
            raw = bytes(new_data) if isinstance(new_data, list) else new_data
            ext = ".otf" if raw[:4] == b"OTTO" else ".ttf"
            fpath = path.join(tmp_dir, stem + ext)
            with open(fpath, "wb") as f:
                f.write(raw)
            return fpath
        if obj_type == "VideoClip" and isinstance(new_data, (bytes, list)):
            fpath = path.join(tmp_dir, stem + ".dat")
            with open(fpath, "wb") as f:
                f.write(bytes(new_data) if isinstance(new_data, list) else new_data)
            return fpath
        if obj_type == "MonoBehaviour" and isinstance(new_data, dict):
            fpath = path.join(tmp_dir, stem + ".json")
            with open(fpath, "wt", encoding="utf-8") as f:
                json.dump(new_data, f, ensure_ascii=False, indent=2)
            return fpath
        return None

    def modify_and_replace(
        self,
        folder_path: str,
        asset_name: str,
        new_data: Any,
        asset_index: Optional[Dict[str, List[dict]]] = None,
        crc_fix: bool = True,
    ) -> None:
        try:
            if os.path.isdir(folder_path):
                if asset_index is not None:
                    all_matches = [m for m in asset_index.get(asset_name, []) if m.get("source_path")]
                else:
                    list_result = self._run_uabea([
                        "list", "-d", folder_path, "-n", f"={asset_name}", "--recursive",
                    ])
                    if list_result.returncode != 0:
                        self._print_error(
                            f"扫描资源失败: {folder_path}\n"
                            f"stderr: {list_result.stderr.strip()}"
                        )
                        return
                    all_matches = [m for m in self._parse_list_output(list_result.stdout) if m["name"] == asset_name]
                if not all_matches:
                    self._print_warning(f"未找到资源: {asset_name}")
                    return
                seen_files = set()
                for match in all_matches:
                    filepath = match.get("source_path", "")
                    if filepath and filepath not in seen_files:
                        seen_files.add(filepath)
                        self._import_single_asset(filepath, match, asset_name, new_data, crc_fix)
                return
            list_result = self._run_uabea(["list", "-f", folder_path, "-n", f"={asset_name}"])
            if list_result.returncode != 0:
                self._print_error(
                    f"扫描资源失败: {folder_path}\n"
                    f"stderr: {list_result.stderr.strip()}"
                )
                return
            matches = [m for m in self._parse_list_output(list_result.stdout) if m["name"] == asset_name]
            if not matches:
                self._print_warning(f"未找到资源: {asset_name}")
                return
            self._import_single_asset(folder_path, matches[0], asset_name, new_data, crc_fix)
        except Exception as e:
            self._print_error(f"修改资源失败: {asset_name}", e)

    def _import_single_asset(
        self,
        filepath: str,
        match: dict,
        asset_name: str,
        new_data: Any,
        crc_fix: bool = True,
    ) -> None:
        try:
            original_crc_int = 0
            if crc_fix:
                codec = CRC32()
                with open(filepath, "rb") as f:
                    consume(codec, f)
                original_crc_int = codec.digest()
            obj_type = match["type"]
            entry = match["entry"]
            path_id = match["path_id"]
            with tempfile.TemporaryDirectory() as tmp_dir:
                import_file = self._new_data_to_file(
                    obj_type, new_data, asset_name, entry, path_id, tmp_dir
                )
                if import_file is None:
                    self._print_error(
                        f"不支持的资源类型或数据格式: type={obj_type}, asset={asset_name}"
                    )
                    return
                imp = self._run_uabea(["import", "-f", filepath, "-i", tmp_dir])
                if imp.returncode != 0:
                    self._print_error(
                        f"UABEA import 失败: {filepath}\n"
                        f"stdout: {imp.stdout.strip()}\n"
                        f"stderr: {imp.stderr.strip()}"
                    )
                    return
            if crc_fix and not self._patch_crc(filepath, original_crc_int):
                self._print_error(f"资源已导入，但 CRC 修补失败: {filepath}")
        except Exception as e:
            self._print_error(f"导入资源失败: {filepath}", e)

    @staticmethod
    def _strip_uabea_suffix(filename: str) -> str:
        """
        清理 UABEA 导出文件名中的 CAB / PathID 等附加信息。
        例如：
            Test-CAB-xxx.png -> Test.png
            Test-xxxxxxxx.png -> Test.png
        """
        name_part, ext = path.splitext(filename)
        if "-CAB-" in name_part:
            return name_part.split("-CAB-", 1)[0] + ext
        parts = name_part.rsplit("-", 2)
        if len(parts) >= 2 and len(parts[-1]) > 8:
            return parts[0] + ext
        return filename

    @staticmethod
    def _remove_extension(filename: str) -> str:
        """去除文件最后一个扩展名。"""
        return path.splitext(filename)[0]

    def extract_bundle(
        self,
        res_path: str,
        extract_types: Optional[List[str]] = None,
        extract_root: str = "output",
        use_type_subdir: bool = True,
        auto_rename_suffix: Optional[List[str]] = None,
    ) -> None:
        """
        提取 Bundle 资源。

        Args:
            res_path: Bundle 文件或 Bundle 目录。
            extract_types: 要提取的资源类型，None 表示 MAIN_EXTRACT_TYPES。
            extract_root: 本次提取的根目录。
            use_type_subdir: 是否按照资源类型创建二级目录。
            auto_rename_suffix: 需要自动添加扩展名的资源类型列表。
                None 表示所有类型均启用。
                例如 ["Texture2D", "Font"] 表示仅这两个类型保留自动扩展名。
        """
        try:
            if not os.path.exists(res_path):
                self._print_error(f"提取源不存在: {res_path}")
                return
            types_to_extract = extract_types or self.MAIN_EXTRACT_TYPES
            suffix_types = set(types_to_extract if auto_rename_suffix is None else auto_rename_suffix)
            extract_root = os.path.abspath(extract_root)
            is_dir = os.path.isdir(res_path)
            path_flag = "-d" if is_dir else "-f"
            self._print_info(f"开始提取: {res_path} | 输出: {extract_root}")
            for obj_type in types_to_extract:
                fmt = self._EXPORT_FORMAT.get(obj_type, "raw")
                with tempfile.TemporaryDirectory() as tmp_dir:
                    export_args = [
                        "export", path_flag, res_path,
                        "-t", obj_type,
                        "-o", tmp_dir,
                        "--format", fmt,
                    ]
                    if is_dir:
                        export_args.append("--recursive")
                    result = self._run_uabea(export_args)
                    exported_files = [
                        f for f in os.listdir(tmp_dir)
                        if path.isfile(path.join(tmp_dir, f))
                    ]
                    if result.returncode != 0:
                        self._print_error(
                            f"提取 {obj_type} 失败\n"
                            f"stdout: {result.stdout.strip()}\n"
                            f"stderr: {result.stderr.strip()}"
                        )
                        continue
                    if not exported_files:
                        self._print_info(f"{obj_type}: 没有找到可提取资源")
                        continue
                    extract_folder = path.join(extract_root, obj_type) if use_type_subdir else extract_root
                    os.makedirs(extract_folder, exist_ok=True)
                    success_count = 0
                    for fname in exported_files:
                        src = path.join(tmp_dir, fname)
                        if obj_type in suffix_types:
                            clean_name = self._strip_uabea_suffix(fname)
                        else:
                            clean_name = self._remove_extension(self._strip_uabea_suffix(fname))
                        dst = path.join(extract_folder, clean_name)
                        if path.exists(dst):
                            self._print_warning(f"目标文件已存在，跳过: {dst}")
                            continue
                        try:
                            shutil.move(src, dst)
                            success_count += 1
                        except Exception as e:
                            self._print_error(f"移动提取文件失败: {src} -> {dst}", e)
                    self._print_info(f"{obj_type}: 成功提取 {success_count}/{len(exported_files)}")
        except Exception as e:
            self._print_error(f"提取 Bundle 失败: {res_path}", e)

    def multiprocess_extract_worker(
        self,
        tasks: multiprocessing.Queue,
        extract_types: Optional[List[str]],
        extract_root: str = "output",
        use_type_subdir: bool = True,
        auto_rename_suffix: Optional[List[str]] = None,
    ) -> None:
        """
        消费 tasks 队列中的 bundle 路径。
        """
        while True:
            try:
                bundle_path = tasks.get_nowait()
            except Exception:
                break
            try:
                ProgressBar.item_text(path.basename(bundle_path))
                self.extract_bundle(
                    bundle_path,
                    extract_types,
                    extract_root,
                    use_type_subdir,
                    auto_rename_suffix,
                )
            except Exception as e:
                self._print_error(f"多进程提取失败: {bundle_path}", e)

    def replace_asset_from_file(
        self,
        folder_path: str,
        asset_name: str,
        file_path: str,
        crc_fix: bool = True,
        asset_index: Optional[Dict[str, List[dict]]] = None,
    ) -> None:
        """
        将 file_path 中的资源替换进 bundle。
        """
        try:
            if not os.path.exists(file_path):
                self._print_error(f"替换文件不存在: {file_path}")
                return
            if os.path.isdir(folder_path):
                if asset_index is not None:
                    all_matches = [
                        m for m in asset_index.get(asset_name, [])
                        if m.get("source_path")
                    ]
                else:
                    list_result = self._run_uabea([
                        "list", "-d", folder_path, "-n", f"={asset_name}", "--recursive",
                    ])
                    if list_result.returncode != 0:
                        self._print_error(
                            f"扫描 Bundle 目录失败: {folder_path}\n"
                            f"stderr: {list_result.stderr.strip()}"
                        )
                        return
                    all_matches = [
                        m for m in self._parse_list_output(list_result.stdout)
                        if m["name"] == asset_name
                    ]
                if not all_matches:
                    self._print_warning(f"未找到资源: {asset_name}")
                    return
                seen_files = set()
                for match in all_matches:
                    target_filepath = match.get("source_path", "")
                    if target_filepath and target_filepath not in seen_files:
                        seen_files.add(target_filepath)
                        self._import_file_direct(
                            target_filepath,
                            match,
                            asset_name,
                            file_path,
                            crc_fix,
                        )
                return
            list_result = self._run_uabea([
                "list", "-f", folder_path, "-n", f"={asset_name}",
            ])
            if list_result.returncode != 0:
                self._print_error(
                    f"扫描 Bundle 文件失败: {folder_path}\n"
                    f"stderr: {list_result.stderr.strip()}"
                )
                return
            matches = [
                m for m in self._parse_list_output(list_result.stdout)
                if m["name"] == asset_name
            ]
            if not matches:
                self._print_warning(f"未找到资源: {asset_name}")
                return
            self._import_file_direct(
                folder_path,
                matches[0],
                asset_name,
                file_path,
                crc_fix,
            )
        except Exception as e:
            self._print_error(f"替换资源失败: {asset_name}", e)

    def _import_file_direct(
        self,
        filepath: str,
        match: dict,
        asset_name: str,
        file_path: str,
        crc_fix: bool,
    ) -> None:
        try:
            if not os.path.isfile(filepath):
                self._print_error(f"目标 Bundle 文件不存在: {filepath}")
                return
            if not os.path.isfile(file_path):
                self._print_error(f"资源文件不存在: {file_path}")
                return
            original_crc_int = 0
            if crc_fix:
                codec = CRC32()
                with open(filepath, "rb") as f:
                    consume(codec, f)
                original_crc_int = codec.digest()
            obj_type = match["type"]
            entry = match["entry"]
            path_id = match["path_id"]
            ext = os.path.splitext(file_path)[1]
            if not ext:
                if obj_type == "TextAsset":
                    ext = ".txt"
                elif obj_type == "Texture2D":
                    ext = ".png"
                elif obj_type == "Font":
                    ext = ".ttf"
                else:
                    ext = ".dat"
            stem = f"{asset_name}-{entry}-{path_id}" if entry else f"{asset_name}-{path_id}"
            with tempfile.TemporaryDirectory() as tmp_dir:
                import_file = os.path.join(tmp_dir, stem + ext)
                shutil.copy2(file_path, import_file)
                imp = self._run_uabea(["import", "-f", filepath, "-i", tmp_dir])
                if imp.returncode != 0:
                    self._print_error(
                        f"UABEA import 失败: {filepath}\n"
                        f"stdout: {imp.stdout.strip()}\n"
                        f"stderr: {imp.stderr.strip()}"
                    )
                    return
            self._print_info(f"资源替换成功: {asset_name} -> {os.path.basename(filepath)}")
            if crc_fix and not self._patch_crc(filepath, original_crc_int):
                self._print_error(f"资源替换成功，但 CRC 修补失败: {filepath}")
        except Exception as e:
            self._print_error(f"导入资源失败: {filepath}", e)
