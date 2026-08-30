import argparse
import base64
import json
import multiprocessing
import os
import re
import shutil
import zipfile
import io

from pathlib import Path
from typing import Optional

from lxml import etree
from distutils.dir_util import copy_tree

from crcmanip.crc import CRC32
from crcmanip.algorithm import apply_patch, consume

from utils.util import CommandUtils, ZipUtils, FileUtils, FileDownloader
from utils.apksigner import ApkSigner
from utils.regions import Server
from xtractor.bundle import (
    BundleExtractor,
    build_asset_index,
    _bundle_replace_worker,
)
from utils.encryption import create_key, convert_string, encrypt_string, xor


# ─────────────────────────── APK 更新器 ──────────────────────────

class ApkUpdater:
    def __init__(
        self,
        repo: str | Path = "BA-APKSRC",
        server: str = "JP",
        workers: int = 4,
    ):
        self.repo = Path(repo)
        self.server = server
        self.workers = max(1, min(workers, os.cpu_count() or 4))

        self.base_dir = Path("Temp")
        self.decoded_path = self.base_dir / "Decoded"
        self.temp_extract_path = self.base_dir / "TempExtract"
        self.main_output_path = self.base_dir / "MainOutput"
        self.apk_path = self.base_dir / f"Temp_{server}.apk"
        self.dex_backup_path = self.base_dir / "DexBackup"

        self.raw_apk = Path("unaligned.apk")
        self.temp_align = Path("temp.apk")
        self.final_apk = Path("蔚蓝档案.apk")

        self.asset_index: dict = {}
        self.official_v1_signatures: dict[str, bytes] = {}

    # ─────────────────────────── 基础 apktool 操作 ──────────────────────────

    def _run_apktool(self, args):
        success, error = CommandUtils.run_command(
            "java", "-jar", str(self.repo / "apktool.jar"), *args
        )
        if not success:
            raise Exception(f"apktool failed: {error}")
        return success

    def extract(self, apk_path, output_dir=None):
        output_dir = Path(output_dir or self.main_output_path)
        if output_dir.exists():
            shutil.rmtree(output_dir)
        print("正在解包……")
        return self._run_apktool(["d", "-f", str(apk_path), "-o", str(output_dir)])

    def build(self, input_dir=None, output_apk=None):
        input_dir = Path(input_dir or self.main_output_path)
        output_apk = Path(output_apk or self.raw_apk)
        print("正在打包……")
        return self._run_apktool(["b", str(input_dir), "-o", str(output_apk)])

    def sign(self, apk_path=None, out_path=None):
        apk_path = Path(apk_path or self.final_apk)
        out_path = Path(out_path or self.final_apk)
        signed_path = Path(str(out_path) + ".signed.tmp.apk")
        signer = ApkSigner(
            apk_path=str(apk_path),
            jks_path=str(self.repo / "beichen.jks"),
            ks_pass="北辰汉化组a",
            key_pass="北辰汉化组a",
            output_path=str(signed_path),
            alias="北辰汉化组",
            min_sdk=28,
            max_sdk=0x7FFFFFFF,
            apksigner_path=str(self.repo / "apksigner.jar"),
        )
        signer.sign()
        if not signed_path.exists():
            raise FileNotFoundError(f"签名输出不存在: {signed_path}")
        os.replace(signed_path, out_path)
        print("签名完成。")
        return True

    # ─────────────────────────── 资源修改 ─────────────────────────────

    def modify_manifest(self, trust_cert: bool = True):
        """ 修改AndroidManifest.xml """
        manifest_path = self.main_output_path / "AndroidManifest.xml"
        content = manifest_path.read_text(encoding="utf-8")

        print("正在合并apk……")
        root = etree.fromstring(content.encode("utf-8"))
        android_ns = "http://schemas.android.com/apk/res/android"

        if trust_cert:
            app_element = root.find(".//application")
            if app_element is not None:
                app_element.set(
                    f"{{{android_ns}}}networkSecurityConfig",
                    "@xml/network_security_config"
                )

        for attr in [
            f"{{{android_ns}}}requiredSplitTypes",
            f"{{{android_ns}}}splitTypes",
        ]:
            root.attrib.pop(attr, None)

        ns = {"android": android_ns}
        for meta in root.findall(".//meta-data", namespaces=ns):
            if meta.get(f"{{{android_ns}}}name") == "com.android.vending.splits.required":
                meta.set(
                    f"{{{android_ns}}}name",
                    "com.android.dynamic.apk.fused.modules"
                )
                meta.set(
                    f"{{{android_ns}}}value",
                    "UnityDataAssetPack,base"
                )

        manifest_path.write_text(
            etree.tostring(root, encoding="utf-8", pretty_print=True).decode("utf-8"),
            encoding="utf-8"
        )
        print("apk合并完成。")

    def modify_resources(self, modifylogin: bool = True, modifygt4: str = "zho"):
        """ 修改resources """
        if modifylogin:
            print("正在修改yostar登录文本。")
            try:
                res_data = json.loads(
                    (self.repo / "resources.json").read_text(encoding="utf-8")
                )
                ja_path = self.main_output_path / "res/values-ja/strings.xml"
                content = ja_path.read_text(encoding="utf-8")
                for item in res_data:
                    content = re.sub(
                        rf'(?s)<string name="{re.escape(item["name"])}">.*?</string>',
                        f'<string name="{item["name"]}">{item["text"]}</string>',
                        content
                    )
                ja_path.write_text(content, encoding="utf-8")
                print("yostar登录文本修改完成。")
            except Exception:
                pass

        if modifygt4:
            print("正在修改极验校验文本。")
            gt4_path = self.main_output_path / "assets" / "gt4.js"
            if gt4_path.exists():
                original_codec = CRC32()
                with gt4_path.open("rb") as f:
                    consume(original_codec, f)
                original_crc_int = original_codec.digest()

                content = gt4_path.read_text(encoding="utf-8")
                old_str = "lang: config.language? config.language : navigator.appName === 'Netscape' ? navigator.language.toLowerCase() : navigator.userLanguage.toLowerCase()"
                if old_str in content:
                    content = content.replace(old_str, f"lang: '{modifygt4}'")

                function_marker = "window.initGeetest4 = function (userConfig,callback) {"
                insert_marker = "    var config = new Config(userConfig);"
                if function_marker not in content or insert_marker not in content:
                    raise RuntimeError("gt4.js 中未找到 initGeetest4 的 CRC 插入位置")

                crc_placeholder = '    userConfig._crcPatch = "";\n'
                insert_at = content.index(insert_marker, content.index(function_marker))
                content = content[:insert_at] + crc_placeholder + content[insert_at:]
                data = content.encode("utf-8")

                patch_marker = b'userConfig._crcPatch = "";'
                target_pos = data.index(patch_marker) + len(b'userConfig._crcPatch = "')

                output_io = io.BytesIO()
                apply_patch(
                    crc=CRC32(),
                    target_checksum=original_crc_int,
                    input_handle=io.BytesIO(data),
                    output_handle=output_io,
                    target_pos=target_pos,
                    overwrite=False,
                )
                gt4_path.write_bytes(output_io.getvalue())

                final_codec = CRC32()
                with gt4_path.open("rb") as f:
                    consume(final_codec, f)
                final_crc = final_codec.digest()

                print(f"  --> gt4.js 原始 CRC: 0x{original_crc_int:08X}")
                print(f"  --> gt4.js 最终 CRC: 0x{final_crc:08X}")
                if final_crc != original_crc_int:
                    raise RuntimeError("gt4.js CRC 修补失败")
                print("gt4登录文本修改完成，CRC修补校验匹配。")

    def modify_sdk_url(self, sdkurl: str):
        print("正在修改SDKConfigSettings.json。")
        sdk_config_path = self.main_output_path / "assets" / "SDKConfigSettings.json"

        original_codec = CRC32()
        with sdk_config_path.open("rb") as f:
            consume(original_codec, f)
        original_crc_int = original_codec.digest()

        sdk_config = json.loads(
            sdk_config_path.read_text(encoding="utf-8")
        )
        sdk_config["Regions"]["Jp"]["Sdk_Url"] = sdkurl
        sdk_config["crc"] = ""

        compact = json.dumps(
            sdk_config,
            separators=(",", ":"),
            ensure_ascii=False
        )
        marker = b'"crc":""'
        data = compact.encode("utf-8")
        target_pos = data.index(marker) + len(b'"crc":') + 1

        codec_before = CRC32()
        consume(codec_before, io.BytesIO(data))
        print(f"\n[DEBUG CRC] 正在处理文件: {sdk_config_path.name}")
        print(f"  --> 原文件目标 CRC (Expected) : 0x{original_crc_int:08X}")
        print(f"  --> 修改后、Patch前 CRC       : 0x{codec_before.digest():08X}")

        output_io = io.BytesIO()
        apply_patch(
            crc=CRC32(),
            target_checksum=original_crc_int,
            input_handle=io.BytesIO(data),
            output_handle=output_io,
            target_pos=target_pos,
            overwrite=False,
        )
        sdk_config_path.write_bytes(output_io.getvalue())

        final_codec = CRC32()
        with sdk_config_path.open("rb") as f:
            consume(final_codec, f)
        final_crc = final_codec.digest()

        print(f"  --> Patch插入后 CRC (Final)    : 0x{final_crc:08X}")
        if final_crc != original_crc_int:
            raise RuntimeError("SDKConfigSettings.json CRC 修补失败")
        print("  --> [成功] CRC 修补校验匹配！\n")
        print("SDKConfigSettings.json修改完成。")

    def modify_game_main_config(self, gamemainconfig: str):
        print("正在修改GameMainConfig。")
        data_folder = str(self.main_output_path / "assets" / "bin" / "Data")
        url_objs = BundleExtractor().search_unity_pack(
            data_folder,
            data_type=["TextAsset"],
            data_name=["GameMainConfig"],
            condition_connect=True,
            collect_index=self.asset_index,
        )

        if not url_objs:
            print("未搜索到GameMainConfig！")
            return

        raw_script = url_objs[0].read().m_Script
        if isinstance(raw_script, str):
            raw_script = raw_script.encode("utf-8", "surrogateescape")

        b64_data = base64.b64encode(raw_script).decode("utf-8")
        raw_json_obj = json.loads(
            convert_string(
                b64_data,
                create_key("GameMainConfig")
            )
        )

        ciphers = {
            "ServerInfoDataUrl": "X04YXBFqd3ZpTg9cKmpvdmpOElwnamB2eE4cXDZqc3ZgTg==",
            "DefaultConnectionGroup": "tSrfb7xhQRKEKtZvrmFjEp4q1G+0YUUSkirOb7NhTxKfKv1vqGFPEoQqym8=",
            "SkipTutorial": "8AOaQvLC5wj3A4RC78L4CNEDmEL6wvsI",
            "Language": "wL4EWsDv8QX5vgRaye/zBQ==",
        }

        gmc_dict = json.loads(gamemainconfig)
        for k, v in gmc_dict.items():
            if k in ciphers:
                raw_json_obj[ciphers[k]] = encrypt_string(
                    v,
                    create_key(k)
                )

        new_raw_script = xor(
            json.dumps(
                raw_json_obj,
                separators=(",", ":")
            ).encode("utf-16le"),
            create_key("GameMainConfig")
        )

        modified_dir = self.repo / "Modified"
        modified_dir.mkdir(parents=True, exist_ok=True)
        (modified_dir / "GameMainConfig").write_bytes(new_raw_script)
        print("GameMainConfig修改完成。")

    def apply_bundle(self):
        print("正在替换Bundle资源。")
        modified_dir = self.repo / "Modified"
        if not modified_dir.exists():
            return

        extractor = BundleExtractor()
        data_folder = str(self.main_output_path / "assets" / "bin" / "Data")

        if not self.asset_index:
            print(f"正在扫描bundle目录建立索引: {data_folder}")
            self.asset_index = build_asset_index(
                extractor,
                data_folder
            )

        print(f"索引资源名数量: {len(self.asset_index)}。")

        tasks = []
        for root, _, files in os.walk(modified_dir):
            for file_name in files:
                file_path = str(Path(root) / file_name)
                asset_name = Path(root, file_name).stem
                matches = [
                    m for m in self.asset_index.get(asset_name, [])
                    if m.get("source_path")
                ]

                if not matches:
                    print(f"[跳过] 未在bundle中找到资源: {asset_name}")
                    continue

                seen_files = set()
                for match in matches:
                    target = match["source_path"]
                    if target and target not in seen_files:
                        seen_files.add(target)
                        tasks.append(
                            (asset_name, target, match, file_path)
                        )

        if not tasks:
            print("没有需要修改的bundle资源。")
            return

        print(
            f"共 {len(tasks)} 个bundle修改任务，"
            f"使用 {self.workers} 进程并行处理……"
        )

        bin_path = extractor.bin_path
        work_items = [
            (bin_path, target, match, asset_name, file_path, True)
            for asset_name, target, match, file_path in tasks
        ]

        with multiprocessing.Pool(processes=self.workers) as pool:
            results = pool.map(_bundle_replace_worker, work_items)

        success = sum(1 for _, ok in results if ok)
        print(f"bundle文件修改完成，成功 {success}/{len(results)}。")

    # ─────────────────────────── 主流程 ─────────────────────────

    def download(self):
        print("正在下载APK。")

        self.base_dir.mkdir(parents=True, exist_ok=True)

        apk_url, version = Server().get_apk_url()
        FileDownloader(
            url=apk_url,
            headers={"User-Agent": "Androidkb"}
        ).save_file(str(self.apk_path))
        print(f"APK版本: {version}")

        return version

    def prepare(self):
        print("正在解压APK。")
        ZipUtils.extract_zip(
            str(self.apk_path),
            str(self.decoded_path / "assets"),
            keywords=["assets/com.YostarJP.BlueArchive"]
        )

        apks = FileUtils.find_files(
            str(self.decoded_path / "assets"),
            ["UnityDataAssetPack", "config", "BlueArchive"]
        )

        main_apk = next(
            a for a in apks
            if "UnityDataAssetPack" not in a
            and "config" not in a
        )
        others = [a for a in apks if a != main_apk]

        print("正在提取APK V1签名校验。")
        # 在清理解包目录前提取 config APK 中的官方 v1 签名，后续打包阶段直接复用。
        config_apk = next(
            (a for a in apks if "config" in a.lower()),
            None
        )

        if not config_apk:
            raise FileNotFoundError(
                "未找到包含官方 v1 签名的 config APK"
            )

        with zipfile.ZipFile(config_apk, "r") as official_zip:
            for name in official_zip.namelist():
                upper_name = name.upper()
                if (
                    upper_name.startswith("META-INF/")
                    and upper_name.rsplit("/", 1)[-1].endswith(
                        (".RSA", ".SF", ".MF")
                    )
                ):
                    self.official_v1_signatures[name] = official_zip.read(name)

        if not self.official_v1_signatures:
            raise FileNotFoundError(
                f"config APK 中未找到官方 v1 签名文件: {config_apk}"
            )

        print("正在备份DEX。")
        # 备份dex
        self.dex_backup_path.mkdir(exist_ok=True)

        with zipfile.ZipFile(main_apk, "r") as z:
            for dex in [
                f for f in z.namelist()
                if f.startswith("classes") and f.endswith(".dex")
            ]:
                (self.dex_backup_path / dex).write_bytes(z.read(dex))

        print("正在合并APK。")
        # 解包主APK并合并其它APK
        self.extract(main_apk, self.main_output_path)
        ZipUtils.extract_zip(others, str(self.temp_extract_path))

        for folder in ["lib", "assets"]:
            src = self.temp_extract_path / folder
            if src.exists():
                copy_tree(
                    str(src),
                    str(self.main_output_path / folder)
                )

        shutil.rmtree(self.decoded_path)
        shutil.rmtree(self.temp_extract_path)
        self.apk_path.unlink()
        print("prepare流程完成。")

    def modify_apktool_yml(self):
        print("正在添加mp4文件压缩。")
        yml_path = self.main_output_path / "apktool.yml"
        yml_content = yml_path.read_text(encoding="utf-8")

        if "doNotCompress:" in yml_content and "- mp4" not in yml_content:
            yml_path.write_text(
                yml_content.replace(
                    "doNotCompress:",
                    "doNotCompress:\n- mp4"
                ),
                encoding="utf-8"
            )

        print("APKToolyml修改完成。")

    def replace_resources(self):
        replace_dir = self.repo / "Replace"
        if replace_dir.exists():
            print("正在替换资源……")
            copy_tree(
                str(replace_dir),
                str(self.main_output_path / "assets")
            )
            print("资源替换完成。")

    def install_trust_cert(self):
        shutil.copy(
            str(self.repo / "network_security_config.xml"),
            str(
                self.main_output_path
                / "res"
                / "xml"
                / "network_security_config.xml"
            )
        )

    def rebuild(self):
        print("正在构建APK。")
        self.build(self.main_output_path, self.raw_apk)

        print("正在恢复时间。")
        # 压缩为apk，并恢复dex。保留官方 APK 的 v1 签名文件。
        TARGET_DATE = (1981, 1, 1, 0, 0, 0)

        with zipfile.ZipFile(self.raw_apk, "r") as zin, zipfile.ZipFile(
            self.temp_align, "w"
        ) as zout:
            for item in zin.infolist():
                if item.filename.startswith("classes") and item.filename.endswith(".dex"):
                    continue

                # apktool/build 可能产生新的 v1 签名文件，必须丢弃，避免覆盖官方文件。
                upper_name = item.filename.upper()
                if (
                    upper_name.startswith("META-INF/")
                    and upper_name.rsplit("/", 1)[-1].endswith(
                        (".RSA", ".SF", ".MF")
                    )
                ):
                    continue

                new_item = zipfile.ZipInfo(item.filename)
                new_item.date_time = TARGET_DATE
                new_item.external_attr = item.external_attr
                new_item.compress_type = item.compress_type
                zout.writestr(
                    new_item,
                    zin.read(item.filename)
                )

            # 使用原版官方 META-INF 签名文件，不重新生成或修改其内容。
            for name, signature_data in self.official_v1_signatures.items():
                new_item = zipfile.ZipInfo(name)
                new_item.date_time = TARGET_DATE
                new_item.compress_type = zipfile.ZIP_STORED
                zout.writestr(new_item, signature_data)

            for dex_file in self.dex_backup_path.iterdir():
                new_item = zipfile.ZipInfo(dex_file.name)
                new_item.date_time = TARGET_DATE
                new_item.compress_type = zipfile.ZIP_DEFLATED
                zout.writestr(
                    new_item,
                    dex_file.read_bytes()
                )

        self.raw_apk.unlink()

        print("正在对齐4字节。")
        # 先将官方 v1 签名文件写入 APK，再执行 zipalign，最后交给自定义 ApkSigner 同时处理签名。
        success, error = CommandUtils.run_command(
            "zipalign",
            "-p",
            "-f",
            "4",
            str(self.temp_align),
            str(self.final_apk)
        )

        if not success:
            raise RuntimeError(f"zipalign failed: {error}")

        self.temp_align.unlink()

        print("正在签名APK。")
        # 使用独立临时 APK 输出签名，完成后再覆盖最终 APK。
        self.sign(self.final_apk, self.final_apk)

    def cleanup(self):
        for path in [
            self.decoded_path,
            self.temp_extract_path,
            self.main_output_path,
            self.dex_backup_path,
        ]:
            if path.exists():
                shutil.rmtree(path)

        if self.apk_path.exists():
            self.apk_path.unlink()

    def run(
        self,
        sdkurl: str = "",
        gamemainconfig: str = "",
        trustcert: bool = False,
        modifylogin: bool = True,
        modifygt4: str = "",
        replace: bool = True,
        modifybundle: bool = True,
    ):
        try:
            self.prepare()

            # 修改压缩规则
            self.modify_apktool_yml()

            # apk合并并修改xml以支持reqable
            self.modify_manifest(trustcert)

            if trustcert:
                self.install_trust_cert()

            # 修改res文件
            self.modify_resources(
                modifylogin,
                modifygt4
            )

            # 替换直接替换的资源文件
            if replace:
                self.replace_resources()

            # 修改sdk
            if sdkurl:
                self.modify_sdk_url(sdkurl)

            # 修改GameMainConfig（同时同步建立全量资源索引，供 bundle 修改复用）
            if gamemainconfig:
                self.modify_game_main_config(gamemainconfig)

            # 修改bundle资源（复用已建立的索引缓存，避免二次扫描）
            if modifybundle:
                self.apply_bundle()

            # 构建、对齐、签名
            self.rebuild()

            print(f"APK更新完成: {self.final_apk}")
        finally:
            self.cleanup()


# ─────────────────────────── 参数 ──────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(description="Update Blue Archive APK")
    parser.add_argument("--server", type=str, default="JP", help="服务器选择")
    parser.add_argument("--sdkurl", type=str, default="", help="修改SDK_Url")
    parser.add_argument("--gamemainconfig", type=str, default="", help="修改GameMainConfig")
    parser.add_argument("--modifylogin", action="store_true", help="修改登录界面语言")
    parser.add_argument("--modifygt4", type=str, default="zho", help="修改登录界面语言")
    parser.add_argument("--replace", action="store_true", help="替换资源")
    parser.add_argument("--modifybundle", action="store_true", help="修改bundle资源")
    parser.add_argument("--repo", type=str, default="BA-APKSRC", help="资源文件夹路径")
    parser.add_argument("--trustcert", action="store_true", help="启用信任证书")
    parser.add_argument("--workers", type=int, default=4, help="bundle修改并行进程数")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()

    updater = ApkUpdater(
        repo=args.repo,
        server=args.server,
        workers=args.workers,
    )

    updater.run(
        sdkurl=args.sdkurl,
        gamemainconfig=args.gamemainconfig,
        trustcert=args.trustcert,
        modifylogin=args.modifylogin,
        modifygt4=args.modifygt4,
        replace=args.replace,
        modifybundle=args.modifybundle,
    )
