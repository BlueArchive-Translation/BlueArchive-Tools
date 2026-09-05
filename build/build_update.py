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
from lxml import etree
from distutils.dir_util import copy_tree
from crcmanip.crc import CRC32
from crcmanip.algorithm import apply_patch, consume

from utils.util import CommandUtils, ZipUtils, FileUtils, FileDownloader
from utils.apksigner import ApkSigner
from utils.regions import Server
from utils.cloudflare import CF
from utils.server import SSHServer
from utils.config import Config
from dotenv import load_dotenv
from datetime import datetime
from zoneinfo import ZoneInfo
from xtractor.bundle import (
    BundleExtractor,
    build_asset_index,
    _bundle_replace_worker,
)
from utils.encryption import (
    create_key,
    convert_string,
    encrypt_string,
    xor,
    crc64_file,
)


# ─────────────────────────── 客户端配置 ──────────────────────────

class ClientConfig:
    SERVERS = {
        "JP": {
            "platform": "Android",
            "data_path": "assets/bin/Data",
            "replace_path": "assets",
            "gt4_path": "assets/gt4.js",
            "sdk_config_path": "assets/SDKConfigSettings.json",
            "modify_login": True,
        },
        "JPiOS": {
            "platform": "iOS",
            "data_path": "Payload/BlueArchive.app/Data",
            "replace_path": "Payload/BlueArchive.app/Data/Raw",
            "gt4_path": "Payload/BlueArchive.app/GTCaptcha4.bundle/gt4.js",
            "sdk_config_path": "Payload/BlueArchive.app/SDKConfigSettings.json",
            "modify_login": False,
        },
        "JPPC": {
            "platform": "Windows",
        },
        "GL": {
            "platform": "Android",
        },
        "GLiOS": {
            "platform": "iOS",
        },
    }

    @classmethod
    def get(cls, server):
        if server not in cls.SERVERS:
            raise ValueError(f"不支持的服务器: {server}")
        return cls.SERVERS[server]


# ─────────────────────────── 客户端更新器 ──────────────────────────

class BuildUpdater:
    def __init__(
        self,
        repo: str | Path = "BA-APKSRC",
        server: str = "JP",
        workers: int = 4,
    ):
        self.repo = Path(repo)
        self.server = server
        self.config = ClientConfig.get(server)
        self.platform = self.config["platform"]
        self.workers = max(1, min(workers, os.cpu_count() or 4))

        self.base_dir = Path("Temp")
        self.decoded_path = self.base_dir / "Decoded"
        self.temp_extract_path = self.base_dir / "TempExtract"
        self.main_output_path = self.base_dir / "MainOutput"
        self.apk_path = self.base_dir / f"Temp_{server}.{'ipa' if self.is_ios else 'apk'}"
        self.dex_backup_path = self.base_dir / "DexBackup"

        self.raw_apk = Path("unaligned.apk")
        self.temp_align = Path("temp.apk")
        self.final_apk = Path("蔚蓝档案.ipa" if self.is_ios else "蔚蓝档案.apk")

        self.asset_index: dict = {}
        self.official_v1_signatures: dict[str, bytes] = {}

    # ─────────────────────────── 平台属性 ──────────────────────────

    @property
    def is_ios(self):
        return self.platform == "iOS"

    @property
    def is_android(self):
        return self.platform == "Android"

    @property
    def is_windows(self):
        return self.platform == "Windows"

    @property
    def data_path(self):
        return self.main_output_path / self.config["data_path"]

    @property
    def replace_path(self):
        return self.main_output_path / self.config["replace_path"]

    @property
    def gt4_path(self):
        return self.main_output_path / self.config["gt4_path"]

    @property
    def sdk_config_path(self):
        return self.main_output_path / self.config["sdk_config_path"]

    # ─────────────────────────── APKTool ──────────────────────────

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

    # ─────────────────────────── APK 签名 ──────────────────────────

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

    # ─────────────────────────── Android Manifest ──────────────────────────

    def modify_manifest(self, trust_cert: bool = True):
        if not self.is_android:
            return

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

    # ─────────────────────────── 登录文本 ──────────────────────────

    def modify_login(self):
        if not self.config.get("modify_login", False):
            return

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

    # ─────────────────────────── GT4 ──────────────────────────

    def modify_gt4(self, modifygt4: str = "zho"):
        if not modifygt4:
            return

        print("正在修改极验校验文本。")
        gt4_path = self.gt4_path

        if not gt4_path.exists():
            print(f"[跳过] 未找到gt4.js: {gt4_path}")
            return

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

    # ─────────────────────────── SDK ──────────────────────────

    def modify_sdk_url(self, sdkurl: str):
        if not sdkurl:
            return

        if self.is_windows:
            print("正在修改Bundle中的SDKConfigSettings。")
            data_folder = str(self.data_path)
            url_objs = BundleExtractor().search_unity_pack(
                data_folder,
                data_type=["TextAsset"],
                data_name=["SDKConfigSettings"],
                condition_connect=True,
                collect_index=self.asset_index,
            )

            if not url_objs:
                print("未搜索到SDKConfigSettings！")
                return

            raw_script = url_objs[0].read().m_Script
            if isinstance(raw_script, str):
                raw_script = raw_script.encode("utf-8", "surrogateescape")

            try:
                sdk_config = json.loads(raw_script.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as e:
                raise RuntimeError(
                    f"SDKConfigSettings解析失败: {e}"
                ) from e

            sdk_config["Regions"]["Jp"]["Sdk_Url"] = sdkurl

            modified_dir = self.repo / "Modified"
            modified_dir.mkdir(parents=True, exist_ok=True)
            (modified_dir / "SDKConfigSettings").write_bytes(
                json.dumps(
                    sdk_config,
                    separators=(",", ":"),
                    ensure_ascii=False
                ).encode("utf-8")
            )
            print("Bundle中的SDKConfigSettings修改完成。")
            return

        print("正在修改SDKConfigSettings.json。")
        sdk_config_path = self.sdk_config_path

        if not sdk_config_path.exists():
            raise FileNotFoundError(
                f"未找到SDKConfigSettings.json: {sdk_config_path}"
            )

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

    # ─────────────────────────── GameMainConfig ──────────────────────────

    def modify_game_main_config(self, gamemainconfig: str):
        if not gamemainconfig:
            return

        print("正在修改GameMainConfig。")
        data_folder = str(self.data_path)
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

    # ─────────────────────────── Windows Prepare ──────────────────────────

    def prepare_windows(self):
        print("正在下载Windows Launcher资源。")

        env_file = Config.env_file.format(server=self.server)
        load_dotenv(env_file, override=True)

        res_ver = os.getenv("ResourceVersion")
        zip_url = os.getenv("ZipConfigUrl")

        if not res_ver:
            raise ValueError(
                f"{env_file} 中未找到 ResourceVersion"
            )

        if not zip_url:
            raise ValueError(
                f"{env_file} 中未找到 ZipConfigUrl"
            )

        print(f"ResourceVersion: {res_ver}")
        print(f"ZipConfigUrl: {zip_url}")

        launcher_dir = self.base_dir / "Launcher"

        Server(self.server).download_launcher_assets(
            res_ver,
            zip_url,
            ["resources.assets", "resources.assets.resS"],
            str(launcher_dir)
        )

        resources_path = launcher_dir / "resources.assets"
        resources_res_path = launcher_dir / "resources.assets.resS"

        if not resources_path.exists():
            raise FileNotFoundError(
                f"未找到resources.assets: {resources_path}"
            )

        if not resources_res_path.exists():
            raise FileNotFoundError(
                f"未找到resources.assets.resS: {resources_res_path}"
            )

        print("正在准备Windows资源。")

        self.main_output_path.mkdir(
            parents=True,
            exist_ok=True
        )

        shutil.copy2(
            resources_path,
            self.main_output_path / "resources.assets"
        )
        shutil.copy2(
            resources_res_path,
            self.main_output_path / "resources.assets.resS"
        )

        print("Windows资源准备完成。")

    # ─────────────────────────── Prepare ──────────────────────────

    def prepare_android(self):
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
        self.dex_backup_path.mkdir(exist_ok=True)

        with zipfile.ZipFile(main_apk, "r") as z:
            for dex in [
                f for f in z.namelist()
                if f.startswith("classes") and f.endswith(".dex")
            ]:
                (self.dex_backup_path / dex).write_bytes(z.read(dex))

        print("正在合并APK。")
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

    def prepare_ios(self):
        print("正在解压IPA。")

        if self.main_output_path.exists():
            shutil.rmtree(self.main_output_path)

        self.main_output_path.mkdir(
            parents=True,
            exist_ok=True
        )

        ZipUtils.extract_zip(
            str(self.apk_path),
            str(self.main_output_path)
        )

        app_path = self.main_output_path / "Payload" / "BlueArchive.app"
        if not app_path.exists():
            raise FileNotFoundError(
                f"未找到BlueArchive.app: {app_path}"
            )

        self.apk_path.unlink()
        print("IPA解包完成。")

    def prepare(self):
        if self.is_windows:
            return self.prepare_windows()
        if self.is_ios:
            return self.prepare_ios()
        return self.prepare_android()

    # ─────────────────────────── APKTool YML ──────────────────────────

    def modify_apktool_yml(self):
        if not self.is_android:
            return

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

    # ─────────────────────────── Replace ──────────────────────────

    def replace_resources(self):
        if self.is_windows:
            return

        replace_dir = self.repo / "Replace"
        if replace_dir.exists():
            print("正在替换资源……")
            copy_tree(
                str(replace_dir),
                str(self.replace_path)
            )
            print("资源替换完成。")

    # ─────────────────────────── Trust Cert ──────────────────────────

    def install_trust_cert(self):
        if not self.is_android:
            return

        shutil.copy(
            str(self.repo / "network_security_config.xml"),
            str(
                self.main_output_path
                / "res"
                / "xml"
                / "network_security_config.xml"
            )
        )

    # ─────────────────────────── Android Rebuild ──────────────────────────

    def rebuild_android(self):
        print("正在构建APK。")
        self.build(self.main_output_path, self.raw_apk)

        print("正在恢复时间。")
        target_date = (1981, 1, 1, 0, 0, 0)

        with zipfile.ZipFile(self.raw_apk, "r") as zin, zipfile.ZipFile(
            self.temp_align, "w"
        ) as zout:
            for item in zin.infolist():
                if item.filename.startswith("classes") and item.filename.endswith(".dex"):
                    continue

                upper_name = item.filename.upper()
                if (
                    upper_name.startswith("META-INF/")
                    and upper_name.rsplit("/", 1)[-1].endswith(
                        (".RSA", ".SF", ".MF")
                    )
                ):
                    continue

                new_item = zipfile.ZipInfo(item.filename)
                new_item.date_time = target_date
                new_item.external_attr = item.external_attr
                new_item.compress_type = item.compress_type
                zout.writestr(
                    new_item,
                    zin.read(item.filename)
                )

            for name, signature_data in self.official_v1_signatures.items():
                new_item = zipfile.ZipInfo(name)
                new_item.date_time = target_date
                new_item.compress_type = zipfile.ZIP_STORED
                zout.writestr(new_item, signature_data)

            for dex_file in self.dex_backup_path.iterdir():
                new_item = zipfile.ZipInfo(dex_file.name)
                new_item.date_time = target_date
                new_item.compress_type = zipfile.ZIP_DEFLATED
                zout.writestr(
                    new_item,
                    dex_file.read_bytes()
                )

        self.raw_apk.unlink()

        print("正在对齐4字节。")
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
        self.sign(self.final_apk, self.final_apk)

    # ─────────────────────────── iOS Rebuild ──────────────────────────

    def rebuild_ios(self):
        print("正在重新打包IPA。")

        if self.final_apk.exists():
            self.final_apk.unlink()

        with zipfile.ZipFile(
            self.final_apk,
            "w",
            zipfile.ZIP_DEFLATED
        ) as zout:
            for root, _, files in os.walk(self.main_output_path):
                for file_name in files:
                    file_path = Path(root) / file_name
                    arcname = file_path.relative_to(
                        self.main_output_path
                    )
                    zout.write(
                        file_path,
                        arcname
                    )

        print(f"IPA打包完成: {self.final_apk}")

    # ─────────────────────────── Windows Rebuild ──────────────────────────

    def rebuild_windows(self):
        print("正在重新打包Windows资源。")

        output_path = Path("蔚蓝档案-Windows")

        if output_path.exists():
            shutil.rmtree(output_path)

        output_path.mkdir(
            parents=True,
            exist_ok=True
        )

        for file_name in [
            "resources.assets",
            "resources.assets.resS",
        ]:
            src = self.main_output_path / file_name
            if not src.exists():
                raise FileNotFoundError(
                    f"未找到Windows资源: {src}"
                )

            shutil.copy2(
                src,
                output_path / file_name
            )

        print(f"Windows资源打包完成: {output_path}")

    def rebuild(self):
        if self.is_windows:
            return self.rebuild_windows()
        if self.is_ios:
            return self.rebuild_ios()
        return self.rebuild_android()

    # ─────────────────────────── Windows Online Config ──────────────────────────

    def _get_windows_online_config(self):
        config_dir = Path("zip_online_config_json")
        if not config_dir.exists():
            raise FileNotFoundError(
                f"未找到在线配置目录: {config_dir}"
            )

        json_files = list(config_dir.glob("*.json"))
        if not json_files:
            raise FileNotFoundError(
                f"未找到在线配置JSON: {config_dir}"
            )

        if len(json_files) > 1:
            raise RuntimeError(
                f"zip_online_config_json 中存在多个JSON文件: {json_files}"
            )

        return json_files[0]

    def _update_online_file(self, file_list, online_path, local_path):
        local_path = Path(local_path)

        if not local_path.exists():
            raise FileNotFoundError(
                f"未找到资源文件: {local_path}"
            )

        file_hash = str(crc64_file(local_path))
        file_size = str(local_path.stat().st_size)

        for item in file_list:
            if item.get("path") == online_path:
                item["hash"] = file_hash
                item["size"] = file_size
                item["modified"] = True
                print(
                    f"  --> {online_path} "
                    f"hash={file_hash} "
                    f"size={file_size}"
                )
                return

        file_list.append({
            "path": online_path,
            "hash": file_hash,
            "size": file_size,
            "modified": True,
        })
        print(
            f"  --> 新增 {online_path} "
            f"hash={file_hash} "
            f"size={file_size}"
        )

    def _update_windows_resource_config(self, file_list):
        print("正在修改Windows资源配置。")

        resources = {
            "/BlueArchive_Data/resources.assets":
                self.main_output_path / "resources.assets",
            "/BlueArchive_Data/resources.assets.resS":
                self.main_output_path / "resources.assets.resS",
        }

        for online_path, local_path in resources.items():
            self._update_online_file(
                file_list,
                online_path,
                local_path
            )

    def _update_windows_replace_config(self, file_list):
        replace_dir = self.repo / "Replace"

        if not replace_dir.exists():
            print("未找到Replace目录，跳过Windows额外资源。")
            return

        print("正在检查Windows Replace资源。")

        for local_path in sorted(
            path for path in replace_dir.rglob("*")
            if path.is_file()
        ):
            relative_path = local_path.relative_to(
                replace_dir
            ).as_posix()

            online_path = (
                "/BlueArchive_Data/StreamingAssets/"
                f"{relative_path}"
            )

            self._update_online_file(
                file_list,
                online_path,
                local_path
            )

    def modify_windows_online_config(self):
        json_path = self._get_windows_online_config()
        print(f"正在修改Windows在线配置: {json_path}")

        data = json.loads(
            json_path.read_text(encoding="utf-8")
        )

        file_list = data.get("file")
        if not isinstance(file_list, list):
            raise ValueError(
                "在线配置JSON中的file不是数组"
            )

        self._update_windows_resource_config(file_list)
        self._update_windows_replace_config(file_list)

        json_path.write_text(
            json.dumps(
                data,
                ensure_ascii=False,
                indent=4
            ) + "\n",
            encoding="utf-8"
        )

        print("Windows在线配置修改完成。")
        return json_path

    # ─────────────────────────── Upload ──────────────────────────

    def _create_ssh_server(self):
        return SSHServer(
            host=os.environ["SERVER_HOST"],
            username="root",
            password=os.environ["SERVER_PASSWORD"],
            port=22
        )

    def _ensure_remote_directory(self, ssh_server, remote_directory):
        print("正在检查远程目录……")

        if not ssh_server.is_dir(remote_directory):
            print(
                f"{remote_directory} 文件夹不存在，正在创建……"
            )
            ssh_server.mkdir(
                remote_directory,
                parents=True
            )
            print("远程文件夹创建成功")
        else:
            print("远程文件夹已存在")

    def _upload_windows(self, ssh_server):
        resource_version = os.environ["ResourceVersion"]
        latest_version = os.environ["LatestVersion"]
        remote_directory = f"/var/www/launcher_download/{resource_version}"
        config_directory = "/var/www/launcher_download/zip_online_config_json"
        self._ensure_remote_directory(ssh_server, remote_directory)
        self._ensure_remote_directory(ssh_server, config_directory)

        print("开始上传Windows客户端资源……")
        windows_output = Path("蔚蓝档案-Windows")
        for file_name in [
            "resources.assets",
            "resources.assets.resS",
        ]:
            local_path = windows_output / file_name
            if not local_path.exists():
                raise FileNotFoundError(f"未找到Windows资源: {local_path}")
            ssh_server.upload_file(
                str(local_path),
                f"{remote_directory}/{file_name}",
                create_parent=False
            )
            print(f"上传完成: {file_name}")

        # 在线配置只修改JSON，不修改或复制Replace资源
        json_path = self.modify_windows_online_config()
        ssh_server.upload_file(
            str(json_path),
            f"{config_directory}/{json_path.name}",
            create_parent=False
        )
        print(f"上传完成: {json_path.name}")
        print("Windows客户端资源上传完成。")
        print("正在更新Cloudflare KV……")

        cf = CF(
            account_id=os.environ["CF_ACCOUNT_ID"],
            api_token=os.environ["CF_API_TOKEN"],
            kv_namespace_id="1f56e1bf592a4ea18d18b2237cdf822d"
        )
        cf.kv.put(
            "Windows_Resource",
            {
                "resourceVersion": latest_version,
                "resourceUpdateTime": datetime.now(
                    ZoneInfo("Asia/Shanghai")
                ).strftime("%Y-%m-%d %H:%M:%S")
            }
        )
        print("KV更新成功")

    def _upload_mobile(self, ssh_server, version):
        remote_directory = "/var/www/web_download"
        self._ensure_remote_directory(ssh_server, remote_directory)
        print(
            "开始上传iOS客户端……"
            if self.is_ios
            else "开始上传Android客户端……"
        )
        remote_name = "蔚蓝档案.ipa" if self.is_ios else "蔚蓝档案.apk"
        ssh_server.upload_file(
            str(self.final_apk),
            f"{remote_directory}/{remote_name}",
            create_parent=False
        )
        print("上传完成")
        print("正在更新Cloudflare KV……")
        cf = CF(
            account_id=os.environ["CF_ACCOUNT_ID"],
            api_token=os.environ["CF_API_TOKEN"],
            kv_namespace_id="1f56e1bf592a4ea18d18b2237cdf822d"
        )
        cf.kv.put(
            "IPA_Resource" if self.is_ios else "APK_Resource",
            {
                "resourceVersion": version,
                "resourceUpdateTime": datetime.now(
                    ZoneInfo("Asia/Shanghai")
                ).strftime("%Y-%m-%d %H:%M:%S")
            }
        )
        print("KV更新成功")

    def upload(self, version):
        print("正在连接服务器……")
        ssh_server = self._create_ssh_server()
        print("正在检查服务器连接……")
        if not ssh_server.test_connection():
            raise RuntimeError("服务器连接失败")
        print("服务器连接成功")
        if self.is_windows:
            return self._upload_windows(ssh_server)
        return self._upload_mobile(ssh_server, version)

    # ─────────────────────────── 主流程 ──────────────────────────

    def run(
        self,
        sdkurl: str = "",
        gamemainconfig: str = "",
        trustcert: bool = False,
        modifylogin: bool = True,
        modifygt4: str = "",
        replace: bool = True,
        modifybundle: bool = True,
        upload: bool = False,
    ):
        try:
            version = self.download() if not self.is_windows else None
            self.prepare()

            if self.is_windows:
                self.modify_sdk_url(sdkurl)

                if gamemainconfig:
                    self.modify_game_main_config(gamemainconfig)

                if modifybundle:
                    self.apply_bundle()

                self.rebuild()
            else:
                self.modify_apktool_yml()
                self.modify_manifest(trustcert)

                if trustcert:
                    self.install_trust_cert()

                if modifylogin:
                    self.modify_login()

                if modifygt4:
                    self.modify_gt4(modifygt4)

                if replace:
                    self.replace_resources()

                if sdkurl:
                    self.modify_sdk_url(sdkurl)

                if gamemainconfig:
                    self.modify_game_main_config(gamemainconfig)

                if modifybundle:
                    self.apply_bundle()

                self.rebuild()

            if upload:
                self.upload(version)

            print(f"客户端更新完成: {self.final_apk}")
        finally:
            self.cleanup()


# ─────────────────────────── 参数 ──────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(description="Update Blue Archive Client")
    parser.add_argument(
        "server",
        choices=["JP", "JPPC", "JPiOS", "GL", "GLiOS"],
        help="选择服务器区域"
    )
    parser.add_argument("--sdkurl", type=str, default="", help="修改SDK_Url")
    parser.add_argument("--gamemainconfig", type=str, default="", help="修改GameMainConfig")
    parser.add_argument("--modifylogin", action="store_true", help="修改Android登录界面语言")
    parser.add_argument("--modifygt4", type=str, default="zho", help="修改登录界面语言")
    parser.add_argument("--replace", action="store_true", help="替换资源")
    parser.add_argument("--modifybundle", action="store_true", help="修改bundle资源")
    parser.add_argument("--repo", type=str, default="BA-APKSRC", help="资源文件夹路径")
    parser.add_argument("--trustcert", action="store_true", help="启用信任证书")
    parser.add_argument("--upload", action="store_true", help="上传客户端到服务器")
    parser.add_argument("--workers", type=int, default=1, help="bundle修改并行进程数")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()

    updater = BuildUpdater(
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
        upload=args.upload,
    )