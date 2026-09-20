import argparse
import base64
import io
import json
import multiprocessing
import os
import re
import shutil
import zipfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from distutils.dir_util import copy_tree
from lxml import etree
from crcmanip.algorithm import apply_patch, consume
from crcmanip.crc import CRC32
from xtractor.bundle import BundleExtractor, build_asset_index, _bundle_replace_worker

from utils.apksigner import ApkSigner
from utils.cloudflare import CF
from utils.config import Config
from utils.encryption import create_key, convert_string, encrypt_string, xor, crc64_file
from utils.regions import Server
from utils.server import SSHServer
from utils.util import CommandUtils, ZipUtils, FileUtils, FileDownloader

class BaseBuilder:
    """所有客户端 Builder 的公共功能。"""
    def __init__(self, repo="BA-APKSRC", server="JP", workers=4):
        self.repo = Path(repo)
        self.server = server
        self.config = Config.servers[server]
        self.workers = max(1, min(workers, os.cpu_count() or 4))
        self.base_dir = Path("Temp")
        self.main_output_path = self.base_dir / "MainOutput"
        self.asset_index = {}
        self.official_v1_signatures = {}
        self.final_path = None

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

    def _create_ssh_server(self):
        return SSHServer(
            host=os.environ["SERVER_HOST"],
            username="root",
            password=os.environ["SERVER_PASSWORD"],
            port=22,
        )

    def _ensure_remote_directory(self, ssh_server, remote_directory):
        if ssh_server.is_dir(remote_directory):
            print(f"远程文件夹已存在: {remote_directory}")
            return
        print(f"远程文件夹不存在，正在创建: {remote_directory}")
        ssh_server.mkdir(remote_directory, parents=True)
        print("远程文件夹创建成功")

    def apply_bundle(self):
        """扫描 Unity Bundle，并替换 Modified 中的资源。"""
        print("正在替换Bundle资源。")
        modified_dir = self.repo / "Modified"
        if not modified_dir.exists():
            print("未找到Modified目录，跳过Bundle修改。")
            return

        extractor = BundleExtractor()
        data_folder = str(self.data_path)
        if not self.asset_index:
            print(f"正在扫描bundle目录建立索引: {data_folder}")
            self.asset_index = build_asset_index(extractor, data_folder)
        print(f"索引资源名数量: {len(self.asset_index)}。")

        tasks = []
        for root, _, files in os.walk(modified_dir):
            for file_name in files:
                file_path = str(Path(root) / file_name)
                asset_name = Path(file_name).stem
                matches = [
                    item for item in self.asset_index.get(asset_name, [])
                    if item.get("source_path")
                ]
                if not matches:
                    print(f"[跳过] 未在bundle中找到资源: {asset_name}")
                    continue
                seen_files = set()
                for match in matches:
                    target = match["source_path"]
                    if target and target not in seen_files:
                        seen_files.add(target)
                        tasks.append((asset_name, target, match, file_path))

        if not tasks:
            print("没有需要修改的bundle资源。")
            return

        print(f"共 {len(tasks)} 个bundle修改任务，使用 {self.workers} 进程并行处理……")
        bin_path = extractor.bin_path
        work_items = [
            (bin_path, target, match, asset_name, file_path, True)
            for asset_name, target, match, file_path in tasks
        ]
        with multiprocessing.Pool(processes=self.workers) as pool:
            results = pool.map(_bundle_replace_worker, work_items)
        success = sum(1 for _, ok in results if ok)
        print(f"bundle文件修改完成，成功 {success}/{len(results)}。")

    def modify_sdk_url(self, sdkurl):
        """修改 Android/iOS/Windows 的 SDK 地址。"""
        if not sdkurl:
            return
        self._modify_sdk_url(sdkurl)

    def _modify_sdk_url(self, sdkurl):
        raise NotImplementedError

    def modify_game_main_config(self, gamemainconfig):
        """修改加密后的 GameMainConfig。"""
        if not gamemainconfig:
            return
        print("正在修改GameMainConfig。")

        url_objs = BundleExtractor().search_unity_pack(
            str(self.data_path),
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
            convert_string(b64_data, create_key("GameMainConfig"))
        )

        ciphers = {
            "ServerInfoDataUrl": "X04YXBFqd3ZpTg9cKmpvdmpOElwnamB2eE4cXDZqc3ZgTg==",
            "DefaultConnectionGroup": "tSrfb7xhQRKEKtZvrmFjEp4q1G+0YUUSkirOb7NhTxKfKv1vqGFPEoQqym8=",
            "SkipTutorial": "8AOaQvLC5wj3A4RC78L4CNEDmEL6wvsI",
            "Language": "wL4EWsDv8QX5vgRaye/zBQ==",
        }

        gmc_dict = json.loads(gamemainconfig)
        for key, value in gmc_dict.items():
            if key in ciphers:
                raw_json_obj[ciphers[key]] = encrypt_string(
                    value,
                    create_key(key),
                )

        new_raw_script = xor(
            json.dumps(
                raw_json_obj,
                separators=(",", ":"),
            ).encode("utf-16le"),
            create_key("GameMainConfig"),
        )

        modified_dir = self.repo / "Modified"
        modified_dir.mkdir(parents=True, exist_ok=True)
        (modified_dir / "GameMainConfig").write_bytes(new_raw_script)
        print("GameMainConfig修改完成。")

    def cleanup(self):
        """清理本次构建产生的临时文件。"""
        for path in [
            self.main_output_path,
            getattr(self, "decoded_path", None),
            getattr(self, "temp_extract_path", None),
            getattr(self, "dex_backup_path", None),
        ]:
            if path and path.exists():
                shutil.rmtree(path)

        for path in [
            getattr(self, "apk_path", None),
            getattr(self, "raw_apk", None),
            getattr(self, "temp_align", None),
        ]:
            if path and path.exists():
                path.unlink()

    def upload(self, version):
        """上传构建结果。"""
        print("正在连接服务器……")
        ssh_server = self._create_ssh_server()
        print("正在检查服务器连接……")
        if not ssh_server.test_connection():
            raise RuntimeError("服务器连接失败")
        print("服务器连接成功")
        self._upload(ssh_server, version)

    def _upload(self, ssh_server, version):
        raise NotImplementedError


class AndroidBuilder(BaseBuilder):
    """Android APK 构建器。"""
    def __init__(self, repo="BA-APKSRC", server="JP", workers=4):
        super().__init__(repo, server, workers)
        self.decoded_path = self.base_dir / "Decoded"
        self.temp_extract_path = self.base_dir / "TempExtract"
        self.dex_backup_path = self.base_dir / "DexBackup"
        self.apk_path = self.base_dir / f"Temp_{server}.apk"
        self.raw_apk = Path("unaligned.apk")
        self.temp_align = Path("temp.apk")
        self.final_path = Path("蔚蓝档案.apk")

    def _run_apktool(self, args):
        success, error = CommandUtils.run_command(
            "java",
            "-jar",
            str(self.repo / "apktool.jar"),
            *args,
        )
        if not success:
            raise RuntimeError(f"apktool failed: {error}")
        return success

    def download(self):
        """下载官方 APK。"""
        print("正在下载APK。")
        self.base_dir.mkdir(parents=True, exist_ok=True)

        apk_url, version = Server(self.server).get_apk_url()
        FileDownloader(
            url=apk_url,
            headers={"User-Agent": "Androidkb"},
        ).save_file(str(self.apk_path))

        print(f"版本: {version}")
        return version

    def extract(self, apk_path, output_dir=None):
        output_dir = Path(output_dir or self.main_output_path)
        if output_dir.exists():
            shutil.rmtree(output_dir)
        print("正在解包……")
        return self._run_apktool([
            "d",
            "-f",
            str(apk_path),
            "-o",
            str(output_dir),
        ])

    def build(self, input_dir=None, output_apk=None):
        input_dir = Path(input_dir or self.main_output_path)
        output_apk = Path(output_apk or self.raw_apk)
        print("正在打包……")
        return self._run_apktool([
            "b",
            str(input_dir),
            "-o",
            str(output_apk),
        ])

    def prepare(self):
        """解压 Split APK 并合并资源。"""
        print("正在解压APK。")
        ZipUtils.extract_zip(
            str(self.apk_path),
            str(self.decoded_path / "assets"),
            keywords=["assets/com.YostarJP.BlueArchive"],
        )

        apks = FileUtils.find_files(
            str(self.decoded_path / "assets"),
            ["UnityDataAssetPack", "config", "BlueArchive"],
        )
        main_apk = next(
            apk for apk in apks
            if "UnityDataAssetPack" not in apk and "config" not in apk
        )
        others = [apk for apk in apks if apk != main_apk]

        print("正在提取APK V1签名校验。")
        config_apk = next(
            (apk for apk in apks if "config" in apk.lower()),
            None,
        )
        if not config_apk:
            raise FileNotFoundError("未找到包含官方 v1 签名的 config APK")

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
        self.dex_backup_path.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(main_apk, "r") as apk_zip:
            for dex in [
                name for name in apk_zip.namelist()
                if name.startswith("classes") and name.endswith(".dex")
            ]:
                (self.dex_backup_path / dex).write_bytes(apk_zip.read(dex))

        print("正在合并APK。")
        self.extract(main_apk)
        ZipUtils.extract_zip(others, str(self.temp_extract_path))

        for folder in ["lib", "assets"]:
            src = self.temp_extract_path / folder
            if src.exists():
                copy_tree(str(src), str(self.main_output_path / folder))

        shutil.rmtree(self.decoded_path)
        shutil.rmtree(self.temp_extract_path)
        self.apk_path.unlink()
        print("prepare流程完成。")

    def modify_manifest(self, trustcert=False):
        print("正在合并apk……")
        manifest_path = self.main_output_path / "AndroidManifest.xml"
        content = manifest_path.read_text(encoding="utf-8")
        root = etree.fromstring(content.encode("utf-8"))
        android_ns = "http://schemas.android.com/apk/res/android"

        if trustcert:
            app_element = root.find(".//application")
            if app_element is not None:
                app_element.set(
                    f"{{{android_ns}}}networkSecurityConfig",
                    "@xml/network_security_config",
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
                    "com.android.dynamic.apk.fused.modules",
                )
                meta.set(
                    f"{{{android_ns}}}value",
                    "UnityDataAssetPack,base",
                )

        manifest_path.write_text(
            etree.tostring(
                root,
                encoding="utf-8",
                pretty_print=True,
            ).decode("utf-8"),
            encoding="utf-8",
        )
        print("apk合并完成。")

    def install_trust_cert(self):
        cert_path = self.main_output_path / "res/xml/network_security_config.xml"
        cert_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(self.repo / "network_security_config.xml", cert_path)

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
                    content,
                )

            ja_path.write_text(content, encoding="utf-8")
            print("yostar登录文本修改完成。")
        except Exception:
            pass

    def modify_gt4(self, modifygt4="zho"):
        if not modifygt4:
            return

        print("正在修改极验校验文本。")
        gt4_path = self.gt4_path
        if not gt4_path.exists():
            print(f"[跳过] 未找到gt4.js: {gt4_path}")
            return

        original_codec = CRC32()
        with gt4_path.open("rb") as file:
            consume(original_codec, file)
        original_crc_int = original_codec.digest()

        content = gt4_path.read_text(encoding="utf-8")
        old_str = (
            "lang: config.language? config.language : "
            "navigator.appName === 'Netscape' ? "
            "navigator.language.toLowerCase() : "
            "navigator.userLanguage.toLowerCase()"
        )
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
        with gt4_path.open("rb") as file:
            consume(final_codec, file)
        final_crc = final_codec.digest()

        print(f"  --> gt4.js 原始 CRC: 0x{original_crc_int:08X}")
        print(f"  --> gt4.js 最终 CRC: 0x{final_crc:08X}")
        if final_crc != original_crc_int:
            raise RuntimeError("gt4.js CRC 修补失败")
        print("gt4登录文本修改完成，CRC修补校验匹配。")

    def _modify_sdk_url(self, sdkurl):
        print("正在修改SDKConfigSettings.json。")
        sdk_config_path = self.sdk_config_path
        if not sdk_config_path.exists():
            raise FileNotFoundError(
                f"未找到SDKConfigSettings.json: {sdk_config_path}"
            )

        original_codec = CRC32()
        with sdk_config_path.open("rb") as file:
            consume(original_codec, file)
        original_crc_int = original_codec.digest()

        sdk_config = json.loads(
            sdk_config_path.read_text(encoding="utf-8")
        )
        sdk_config["Regions"]["Jp"]["Sdk_Url"] = sdkurl
        sdk_config["crc"] = ""

        compact = json.dumps(
            sdk_config,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        data = compact.encode("utf-8")
        marker = b'"crc":""'
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
        with sdk_config_path.open("rb") as file:
            consume(final_codec, file)
        final_crc = final_codec.digest()

        print(f"  --> Patch插入后 CRC (Final)    : 0x{final_crc:08X}")
        if final_crc != original_crc_int:
            raise RuntimeError("SDKConfigSettings.json CRC 修补失败")
        print("  --> [成功] CRC 修补校验匹配！\n")
        print("SDKConfigSettings.json修改完成。")

    def modify_apktool_yml(self):
        print("正在添加mp4文件压缩。")
        yml_path = self.main_output_path / "apktool.yml"
        yml_content = yml_path.read_text(encoding="utf-8")

        if "doNotCompress:" in yml_content and "- mp4" not in yml_content:
            yml_path.write_text(
                yml_content.replace(
                    "doNotCompress:",
                    "doNotCompress:\n- mp4",
                ),
                encoding="utf-8",
            )
        print("APKToolyml修改完成。")

    def replace_resources(self):
        replace_dir = self.repo / "Replace"
        if not replace_dir.exists():
            return

        print("正在替换资源……")
        copy_tree(str(replace_dir), str(self.replace_path))
        print("资源替换完成。")

    def rebuild(self):
        """重新构建、恢复官方签名文件并重新签名。"""
        print("正在构建APK。")
        self.build(self.main_output_path, self.raw_apk)

        print("正在恢复时间。")
        target_date = (1981, 1, 1, 0, 0, 0)

        with zipfile.ZipFile(self.raw_apk, "r") as zin, zipfile.ZipFile(
            self.temp_align,
            "w",
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
                zout.writestr(new_item, zin.read(item.filename))

            for name, signature_data in self.official_v1_signatures.items():
                new_item = zipfile.ZipInfo(name)
                new_item.date_time = target_date
                new_item.compress_type = zipfile.ZIP_STORED
                zout.writestr(new_item, signature_data)

            for dex_file in self.dex_backup_path.iterdir():
                new_item = zipfile.ZipInfo(dex_file.name)
                new_item.date_time = target_date
                new_item.compress_type = zipfile.ZIP_DEFLATED
                zout.writestr(new_item, dex_file.read_bytes())

        self.raw_apk.unlink()

        print("正在对齐4字节。")
        success, error = CommandUtils.run_command(
            "zipalign",
            "-p",
            "-f",
            "4",
            str(self.temp_align),
            str(self.final_path),
        )
        if not success:
            raise RuntimeError(f"zipalign failed: {error}")

        self.temp_align.unlink()
        print("正在签名APK。")
        self.sign()

    def sign(self):
        signed_path = Path(str(self.final_path) + ".signed.tmp.apk")
        signer = ApkSigner(
            apk_path=str(self.final_path),
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

        os.replace(signed_path, self.final_path)
        print("签名完成。")

    def _upload(self, ssh_server, version):
        remote_directory = "/var/www/web_download"
        self._ensure_remote_directory(ssh_server, remote_directory)

        print("开始上传Android客户端……")
        ssh_server.upload_file(
            str(self.final_path),
            f"{remote_directory}/蔚蓝档案.apk",
            create_parent=False,
        )
        print("上传完成")

        self._update_mobile_kv("APK_Resource", version)

    def _update_mobile_kv(self, key, version):
        print("正在更新Cloudflare KV……")
        cf = CF(
            account_id=os.environ["CF_ACCOUNT_ID"],
            api_token=os.environ["CF_API_TOKEN"],
            kv_namespace_id="1f56e1bf592a4ea18d18b2237cdf822d",
        )
        cf.kv.put(
            key,
            {
                "resourceVersion": version,
                "resourceUpdateTime": datetime.now(
                    ZoneInfo("Asia/Shanghai")
                ).strftime("%Y-%m-%d %H:%M:%S"),
            },
        )
        print("KV更新成功")

    def run(
        self,
        sdkurl="",
        gamemainconfig="",
        trustcert=False,
        modifylogin=True,
        modifygt4="",
        replace=True,
        modifybundle=True,
        upload=False,
    ):
        try:
            version = self.download()
            self.prepare()
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

            print(f"客户端更新完成: {self.final_path}")
        finally:
            self.cleanup()


class IOSBuilder(BaseBuilder):
    """iOS IPA 构建器。"""
    def __init__(self, repo="BA-APKSRC", server="JPiOS", workers=4):
        super().__init__(repo, server, workers)
        self.apk_path = self.base_dir / f"Temp_{server}.ipa"
        self.final_path = Path("蔚蓝档案.ipa")

    def download(self):
        """下载官方 IPA。"""
        print("正在下载IPA。")
        self.base_dir.mkdir(parents=True, exist_ok=True)

        apk_url, version = Server(self.server).get_apk_url()
        FileDownloader(
            url=apk_url,
            headers={"User-Agent": "Androidkb"},
        ).save_file(str(self.apk_path))

        print(f"版本: {version}")
        return version

    def prepare(self):
        """解包 IPA。"""
        print("正在解压IPA。")
        if self.main_output_path.exists():
            shutil.rmtree(self.main_output_path)

        self.main_output_path.mkdir(
            parents=True,
            exist_ok=True,
        )
        ZipUtils.extract_zip(
            str(self.apk_path),
            str(self.main_output_path),
        )

        app_path = self.main_output_path / "Payload" / "BlueArchive.app"
        if not app_path.exists():
            raise FileNotFoundError(f"未找到BlueArchive.app: {app_path}")

        self.apk_path.unlink()
        print("IPA解包完成。")

    def _modify_sdk_url(self, sdkurl):
        print("正在修改SDKConfigSettings.json。")
        sdk_config_path = self.sdk_config_path
        if not sdk_config_path.exists():
            raise FileNotFoundError(
                f"未找到SDKConfigSettings.json: {sdk_config_path}"
            )

        original_codec = CRC32()
        with sdk_config_path.open("rb") as file:
            consume(original_codec, file)
        original_crc_int = original_codec.digest()

        sdk_config = json.loads(
            sdk_config_path.read_text(encoding="utf-8")
        )
        sdk_config["Regions"]["Jp"]["Sdk_Url"] = sdkurl
        sdk_config["crc"] = ""

        data = json.dumps(
            sdk_config,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")

        marker = b'"crc":""'
        target_pos = data.index(marker) + len(b'"crc":') + 1

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
        with sdk_config_path.open("rb") as file:
            consume(final_codec, file)

        final_crc = final_codec.digest()
        print(f"  --> 原始 CRC: 0x{original_crc_int:08X}")
        print(f"  --> 最终 CRC: 0x{final_crc:08X}")

        if final_crc != original_crc_int:
            raise RuntimeError("SDKConfigSettings.json CRC 修补失败")
        print("SDKConfigSettings.json修改完成。")

    def rebuild(self):
        """重新打包 IPA。"""
        print("正在重新打包IPA。")
        if self.final_path.exists():
            self.final_path.unlink()

        with zipfile.ZipFile(
            self.final_path,
            "w",
            zipfile.ZIP_DEFLATED,
        ) as zout:
            for root, _, files in os.walk(self.main_output_path):
                for file_name in files:
                    file_path = Path(root) / file_name
                    arcname = file_path.relative_to(self.main_output_path)
                    zout.write(file_path, arcname)

        print(f"IPA打包完成: {self.final_path}")

    def _upload(self, ssh_server, version):
        remote_directory = "/var/www/web_download"
        self._ensure_remote_directory(ssh_server, remote_directory)

        print("开始上传iOS客户端……")
        ssh_server.upload_file(
            str(self.final_path),
            f"{remote_directory}/蔚蓝档案.ipa",
            create_parent=False,
        )
        print("上传完成")

        print("正在更新Cloudflare KV……")
        cf = CF(
            account_id=os.environ["CF_ACCOUNT_ID"],
            api_token=os.environ["CF_API_TOKEN"],
            kv_namespace_id="1f56e1bf592a4ea18d18b2237cdf822d",
        )
        cf.kv.put(
            "IPA_Resource",
            {
                "resourceVersion": version,
                "resourceUpdateTime": datetime.now(
                    ZoneInfo("Asia/Shanghai")
                ).strftime("%Y-%m-%d %H:%M:%S"),
            },
        )
        print("KV更新成功")

    def run(
        self,
        sdkurl="",
        gamemainconfig="",
        modifybundle=True,
        upload=False,
    ):
        try:
            version = self.download()
            self.prepare()

            if sdkurl:
                self.modify_sdk_url(sdkurl)
            if gamemainconfig:
                self.modify_game_main_config(gamemainconfig)
            if modifybundle:
                self.apply_bundle()

            self.rebuild()

            if upload:
                self.upload(version)

            print(f"客户端更新完成: {self.final_path}")
        finally:
            self.cleanup()


class WindowsBuilder(BaseBuilder):
    """Windows 客户端资源构建器。"""
    def __init__(self, repo="BA-APKSRC", server="JPPC", workers=4):
        super().__init__(repo, server, workers)
        self.data_path = self.base_dir / self.config["data_path"]
        self.final_path = self.data_path

    def prepare(self):
        """准备 Unity Windows 资源。"""
        print("正在准备Windows Launcher资源。")

        env_file = Config.env_file.format(server=self.server)
        load_dotenv(env_file, override=True)

        res_ver = os.getenv("ResourceVersion")
        zip_url = os.getenv("ZipConfigUrl")
        if not res_ver:
            raise ValueError(f"{env_file} 中未找到 ResourceVersion")
        if not zip_url:
            raise ValueError(f"{env_file} 中未找到 ZipConfigUrl")

        print(f"ResourceVersion: {res_ver}")
        print(f"ZipConfigUrl: {zip_url}")

        resources_path = self.data_path / "resources.assets"
        resources_res_path = self.data_path / "resources.assets.resS"

        if resources_path.exists() and resources_res_path.exists():
            print(f"检测到已有Windows资源，直接使用: {self.data_path}")
            return

        launcher_dir = self.base_dir / "Launcher"
        Server(self.server).download_launcher_assets(
            res_ver,
            zip_url,
            ["resources.assets", "resources.assets.resS"],
            str(launcher_dir),
        )

        launcher_resources_path = launcher_dir / "resources.assets"
        launcher_resources_res_path = launcher_dir / "resources.assets.resS"

        if not launcher_resources_path.exists():
            raise FileNotFoundError(
                f"未找到resources.assets: {launcher_resources_path}"
            )
        if not launcher_resources_res_path.exists():
            raise FileNotFoundError(
                f"未找到resources.assets.resS: {launcher_resources_res_path}"
            )

        print("正在准备Windows资源。")
        self.data_path.mkdir(parents=True, exist_ok=True)
        shutil.copy2(launcher_resources_path, resources_path)
        shutil.copy2(launcher_resources_res_path, resources_res_path)
        print(f"Windows资源准备完成: {self.data_path}")

    def _modify_sdk_url(self, sdkurl):
        print("正在修改Bundle中的SDKConfigSettings。")

        url_objs = BundleExtractor().search_unity_pack(
            str(self.data_path),
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
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeError(f"SDKConfigSettings解析失败: {error}") from error

        sdk_config["Regions"]["Jp"]["Sdk_Url"] = sdkurl

        modified_dir = self.repo / "Modified"
        modified_dir.mkdir(parents=True, exist_ok=True)
        (modified_dir / "SDKConfigSettings").write_bytes(
            json.dumps(
                sdk_config,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        )
        print("Bundle中的SDKConfigSettings修改完成。")

    def _get_online_config(self):
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
        """更新在线配置中的文件 hash、size 和 modified 标记。"""
        local_path = Path(local_path)
        if not local_path.exists():
            raise FileNotFoundError(f"未找到资源文件: {local_path}")

        file_hash = str(crc64_file(local_path))
        file_size = str(local_path.stat().st_size)

        for item in file_list:
            if item.get("path") == online_path:
                item["hash"] = file_hash
                item["size"] = file_size
                item["modified"] = True
                print(f"  --> {online_path} hash={file_hash} size={file_size}")
                return

        file_list.append({
            "path": online_path,
            "hash": file_hash,
            "size": file_size,
            "modified": True,
        })
        print(f"  --> 新增 {online_path} hash={file_hash} size={file_size}")

    def _update_resource_config(self, file_list):
        print("正在修改Windows资源配置。")

        resources = {
            "/BlueArchive_Data/resources.assets": self.data_path / "resources.assets",
            "/BlueArchive_Data/resources.assets.resS": self.data_path / "resources.assets.resS",
        }
        for online_path, local_path in resources.items():
            self._update_online_file(
                file_list,
                online_path,
                local_path,
            )

    def _update_replace_config(self, file_list):
        replace_dir = self.repo / "Replace"
        if not replace_dir.exists():
            print("未找到Replace目录，跳过Windows额外资源。")
            return

        print("正在检查Windows Replace资源。")
        for local_path in sorted(
            path for path in replace_dir.rglob("*")
            if path.is_file()
        ):
            relative_path = local_path.relative_to(replace_dir).as_posix()
            online_path = (
                "/BlueArchive_Data/StreamingAssets/"
                f"{relative_path}"
            )
            self._update_online_file(
                file_list,
                online_path,
                local_path,
            )

    def modify_online_config(self):
        """修改 Launcher 的在线资源配置。"""
        json_path = self._get_online_config()
        print(f"正在修改Windows在线配置: {json_path}")

        data = json.loads(
            json_path.read_text(encoding="utf-8")
        )
        file_list = data.get("file")
        if not isinstance(file_list, list):
            raise ValueError("在线配置JSON中的file不是数组")

        self._update_resource_config(file_list)
        self._update_replace_config(file_list)

        json_path.write_text(
            json.dumps(
                data,
                ensure_ascii=False,
                indent=4,
            ) + "\n",
            encoding="utf-8",
        )
        print("Windows在线配置修改完成。")
        return json_path

    def rebuild(self):
        """Windows 不需要重新打包，只需确认资源存在。"""
        print("正在检查Windows资源。")
        if not self.data_path.exists():
            raise FileNotFoundError(
                f"未找到Windows资源目录: {self.data_path}"
            )

        for file_name in [
            "resources.assets",
            "resources.assets.resS",
        ]:
            file_path = self.data_path / file_name
            if not file_path.exists():
                raise FileNotFoundError(
                    f"未找到Windows资源: {file_path}"
                )

        print(f"Windows资源准备完成: {self.data_path}")

    def _upload(self, ssh_server, version):
        resource_version = os.environ["ResourceVersion"]
        latest_version = os.environ["LatestVersion"]

        remote_directory = (
            f"/var/www/launcher_download/{resource_version}"
        )
        config_directory = (
            "/var/www/launcher_download/zip_online_config_json"
        )

        self._ensure_remote_directory(
            ssh_server,
            remote_directory,
        )
        self._ensure_remote_directory(
            ssh_server,
            config_directory,
        )

        print("开始上传Windows客户端资源。")
        for file_name in [
            "resources.assets",
            "resources.assets.resS",
        ]:
            local_path = self.data_path / file_name
            if not local_path.exists():
                raise FileNotFoundError(
                    f"未找到Windows资源: {local_path}"
                )

            ssh_server.upload_file(
                str(local_path),
                f"{remote_directory}/{file_name}",
                create_parent=False,
            )
            print(f"上传完成: {file_name}")

        json_path = self.modify_online_config()
        ssh_server.upload_file(
            str(json_path),
            f"{config_directory}/{json_path.name}",
            create_parent=False,
        )
        print(f"上传完成: {json_path.name}")
        print("Windows客户端资源上传完成。")

        print("正在更新Cloudflare KV……")
        cf = CF(
            account_id=os.environ["CF_ACCOUNT_ID"],
            api_token=os.environ["CF_API_TOKEN"],
            kv_namespace_id="1f56e1bf592a4ea18d18b2237cdf822d",
        )
        cf.kv.put(
            "Windows_Resource",
            {
                "resourceVersion": latest_version,
                "resourceUpdateTime": datetime.now(
                    ZoneInfo("Asia/Shanghai")
                ).strftime("%Y-%m-%d %H:%M:%S"),
            },
        )
        print("KV更新成功")

    def run(
        self,
        sdkurl="",
        gamemainconfig="",
        modifybundle=True,
        upload=False,
    ):
        try:
            self.prepare()

            if sdkurl:
                self.modify_sdk_url(sdkurl)
            if gamemainconfig:
                self.modify_game_main_config(gamemainconfig)
            if modifybundle:
                self.apply_bundle()

            self.rebuild()

            if upload:
                self.upload(None)

            print(f"客户端更新完成: {self.final_path}")
        finally:
            self.cleanup()

def parse_args():
    parser = argparse.ArgumentParser(
        description="Update Blue Archive Client"
    )
    parser.add_argument(
        "server",
        choices=["JP", "JPPC", "JPiOS", "GL", "GLiOS"],
        help="选择服务器区域",
    )
    parser.add_argument(
        "--sdkurl",
        type=str,
        default="",
        help="修改SDK_Url",
    )
    parser.add_argument(
        "--gamemainconfig",
        type=str,
        default="",
        help="修改GameMainConfig",
    )
    parser.add_argument(
        "--modifylogin",
        action="store_true",
        help="修改Android登录界面语言",
    )
    parser.add_argument(
        "--modifygt4",
        type=str,
        default="zho",
        help="修改登录界面语言",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="替换资源",
    )
    parser.add_argument(
        "--modifybundle",
        action="store_true",
        help="修改bundle资源",
    )
    parser.add_argument(
        "--repo",
        type=str,
        default="BA-APKSRC",
        help="资源文件夹路径",
    )
    parser.add_argument(
        "--trustcert",
        action="store_true",
        help="启用信任证书",
    )
    parser.add_argument(
        "--upload",
        action="store_true",
        help="上传客户端到服务器",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="bundle修改并行进程数",
    )
    return parser.parse_args()
