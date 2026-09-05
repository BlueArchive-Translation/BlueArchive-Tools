import os
import shutil
import base64
import json
import re
import time
import hashlib
from dotenv import load_dotenv
from utils.encryption import create_key, convert_string
from utils.console import notice
from utils.config import Config
from utils.util import ZipUtils, FileUtils, AsarUtils, CommandUtils, FileDownloader
from xtractor.bundle import BundleExtractor

class Server:
    SERVERS = {
        "JP": {
            "platform": "Android",
            "data_path": "assets/bin/Data",
            "replace_path": "assets",
        },
        "JPiOS": {
            "platform": "iOS",
            "data_path": "Payload/BlueArchive.app/Data",
            "replace_path": "Payload/BlueArchive.app/Data/Raw",
        },
        "JPPC": {
            "platform": "Windows",
            "data_path": "BlueArchive_Data",
            "replace_path": "BlueArchive_Data",
        },
        "CN": {
            "platform": "Android",
            "data_path": "assets/bin/Data",
            "replace_path": "assets",
        },
        # 预留
        # "GL": {
        #     "platform": "Android",
        #     "data_path": "assets/bin/Data",
        #     "replace_path": "assets",
        # },
        # "GLiOS": {
        #     "platform": "iOS",
        #     "data_path": "Payload/BlueArchive.app/Data",
        #     "replace_path": "Payload/BlueArchive.app/Data/Raw",
        # },
    }

    def __init__(self, server):
        self.server = server
        self.config = self.SERVERS.get(server, {})
        self.platform = self.config.get("platform")
        self.data_path = self.config.get("data_path")
        self.replace_path = self.config.get("replace_path")

    def main(self, apk_url):
        """ 大版本更新提取包体并解压 """
        if self.server in ("JP", "GL", "CN"):
            downloader_name = f"BlueArchive_{self.server}_Downdloader.apk"
            Temp_name = f"Temp_{self.server}_Downloader"
            if os.path.exists(downloader_name):
                notice(f"检测到本地已存在 {downloader_name}，跳过下载。")
            else:
                notice("开始下载APK文件。")
            FileDownloader(url=apk_url, headers={"User-Agent": "Androidkb"}, verbose=True).save_file(downloader_name)
            if self.server == "CN":
                ZipUtils.extract_zip(zip_path=downloader_name, dest_dir="Temp")
            else:
                ZipUtils.extract_zip(zip_path=downloader_name, dest_dir=Temp_name)
                apk_files = FileUtils.find_files(Temp_name, [r".*\.apk$"], sequential_match=False)
                print(f"找到的文件: {apk_files}")
                for apk in apk_files:
                    ZipUtils.extract_zip(zip_path=apk, dest_dir="Temp")
                shutil.rmtree(Temp_name)
            os.remove(downloader_name)

        elif self.server == "JPPC":
            exe_name = "BlueArchive.exe"
            temp_exe_dir = f"Temp_{self.server}"
            temp_app_dir = f"Temp_{self.server}_App"

            FileDownloader(url=apk_url, verbose=True).save_file(exe_name)

            CommandUtils.run_command("7z", "x", exe_name, f"-o{temp_exe_dir}", "-y")

            original_asar_path = os.path.abspath(
                os.path.join(temp_exe_dir, "$PLUGINSDIR", "resources", "app.asar")
            )

            app_7z_path = os.path.abspath(
                os.path.join(temp_exe_dir, "$PLUGINSDIR", "app-32.7z")
            )
            CommandUtils.run_command("7z", "x", app_7z_path, f"-o{temp_app_dir}", "-y")

            modified_asar_path = os.path.abspath(
                os.path.join(temp_app_dir, "resources", "app.asar")
            )

            os.makedirs("BA-PC-SRC", exist_ok=True)
            os.makedirs("BA-PC-SRC-INSTALL", exist_ok=True)

            AsarUtils.extract_asar(
                asar_path=modified_asar_path,
                dest_dir="BA-PC-SRC"
            )
            AsarUtils.extract_asar(
                asar_path=original_asar_path,
                dest_dir="BA-PC-SRC-INSTALL"
            )

            shutil.rmtree(temp_exe_dir)
            shutil.rmtree(temp_app_dir)
            os.remove(exe_name)

        elif self.server == "JPiOS":
            FileDownloader(url=apk_url, verbose=True).save_file("BlueArchive.ipa")
            ZipUtils.extract_zip(zip_path="BlueArchive.ipa", dest_dir="Temp")

    def get_apk_url(self):
        """ 获取apk链接并返回url和版本 """
        if self.server in ("JP", "GL", "CN"):
            server_id = {
                "JP": 124755,
                "GL": 139059,
                "CN": 151329
            }
            game_id = server_id.get(self.server)
            url = f"https://api.3839app.com/cdn/android/gameintro-home-1546-id-{game_id}-packag--level-2.htm"
            response = FileDownloader(url=url).get_response()
            downinfo = response.json().get("result", {}).get("data", {}).get("downinfo", {})
            apk_url = downinfo.get("apkurl")
            version = downinfo.get("version")

        elif self.server == "JPPC":
            html = FileDownloader("https://bluearchive.jp/").get_response().text
            app_js = re.search(r'https://webusstatic\.yo-star\.com/bluearchive_jp_web/js/app\.[0-9a-f]+\.js', html).group(0)
            js_content = FileDownloader(app_js).get_response().text
            match = re.search(r'(https://[^\s"\'()]+BlueArchive_JP_Gamelauncher-([0-9.]+)-setup\.exe)', js_content)
            apk_url = match.group(1)
            version = match.group(2)

        elif self.server in ("JPiOS", "GLiOS"):
            ios_info = {
                "JPiOS": (1515877221, "jp"),
                "GLiOS": (1571873795, "tw")
            }
            game_id, country = ios_info[self.server]
            url = f"https://itunes.apple.com/lookup?id={game_id}&country={country}"
            response = FileDownloader(url=url).get_response()
            results = response.json().get("results", [])
            apk_url = None
            version = results[0].get("version")

        return apk_url, version

    def get_game_main_config(self, files_path) -> str:
        """ 获取GameMainConfig并返回json """
        extractor = BundleExtractor(install_dir="tools", EXTRACT_DIR="Extracted")
        config_data = {}
        if self.server == "GL":
            return config_data

        data_folder = str(os.path.join(files_path, self.data_path))
        url_objs = extractor.search_unity_pack(
            data_folder,
            data_type=["TextAsset"],
            data_name=["GameMainConfig"],
            condition_connect=True,
        )

        if url_objs:
            raw_script = url_objs[0].read().m_Script
            if self.server in ("JP", "JPPC", "JPiOS"):
                ciphers = {
                    "ServerInfoDataUrl": "X04YXBFqd3ZpTg9cKmpvdmpOElwnamB2eE4cXDZqc3ZgTg==",
                    "DefaultConnectionGroup": "tSrfb7xhQRKEKtZvrmFjEp4q1G+0YUUSkirOb7NhTxKfKv1vqGFPEoQqym8=",
                    "SkipTutorial": "8AOaQvLC5wj3A4RC78L4CNEDmEL6wvsI",
                    "Language": "wL4EWsDv8QX5vgRaye/zBQ==",
                }
            elif self.server == "CN":
                ciphers = {
                    "ServerInfoDataUrl": "X04YXBFqd3ZpTg9cKmpvdmpOElwnamB2eE4cXDZqc3ZgTg==",
                    "SkipTutorial": "8AOaQvLC5wj3A4RC78L4CNEDmEL6wvsI",
                    "ServerName": "ioIcSFNXEmG8ggtIb1cFYbSCHEg=",
                    "VersionCode": "nYFvU65AaVWigWVTskBZVaSBblO5QA==",
                    "PlatformID": "wBgDdzgJJFH2GAB3Kwk9UdkYK3c=",
                    "ChannelID": "SiMCCCkfsBxnIw8IJB+XHE0j",
                }
            else:
                ciphers = {}

            b64_data = base64.b64encode(raw_script).decode("utf-8")
            json_str = convert_string(b64_data, create_key("GameMainConfig"))
            raw_json_obj = json.loads(json_str)

            for key, cipher_key in ciphers.items():
                if cipher_key in raw_json_obj:
                    encrypted_value = raw_json_obj[cipher_key]
                    config_data[key] = convert_string(encrypted_value, create_key(key))
        return config_data

    def get_server_url(self, version) -> str:
        """ 获取serverinfo """
        if self.server in ("JP", "JPPC", "JPiOS"):
            config_data = self.get_game_main_config("Temp")
            server_url = config_data.get("ServerInfoDataUrl")
            if not server_url:
                server_url = None
                print("未获取到Serverinfo！")
            return server_url, None, None

        elif self.server == "GL":
            build_number = version.split(".")[-1]
            body = {
                "market_game_id": "com.nexon.bluearchive",
                "market_code": "playstore",
                "curr_build_version": version,
                "curr_build_number": build_number
            }
            print(f"[*] 正在向服务器请求版本: {version} (Build: {build_number})")
            downloader = FileDownloader("https://api-pub.nexon.com/patch/v1.1/version-check", request_method="post", json=body)
            resp = downloader.get_response()
            if resp and resp.status_code == 200:
                data = resp.json()
                resource_path = data.get("patch", {}).get("resource_path")
                notice("获取成功。")
                return resource_path, None, None
            else:
                notice("请求失败。")
                return None, None, None

        elif self.server == "CN":
            config_data = self.get_game_main_config("Temp")
            server_url = "https://gs-api.bluearchive-cn.com/api/state"
            platform_id = config_data.get("PlatformID")
            channel_id = config_data.get("ChannelID")
            return server_url, platform_id, channel_id

    def get_addressable_catalog_url(self, server_url: str, platform_id: str = None, channel_id: str = None, version: str = None) -> str:
        if not server_url:
            notice(f"[ERROR] get_addressable_catalog_url 收到空的 server_url (server={self.server})，尝试使用环境变量中缓存的地址。")
            return None, None, None, None, None

        if self.server in ("JP", "JPPC", "JPiOS"):
            downloader = FileDownloader(server_url)
            data = downloader.get_response().json()
            connection_groups = data.get("ConnectionGroups", [])
            override_groups = connection_groups[0].get("OverrideConnectionGroups", [])
            latest_catalog_url = override_groups[-1].get("AddressablesCatalogUrlRoot")
            return latest_catalog_url, None, None, None, None

        elif self.server == "GL":
            latest_catalog_url = server_url.rsplit("/", 1)[0] if server_url and "/" in server_url else None
            return latest_catalog_url, None, None, None, None

        elif self.server == "CN":
            headers = {
                "APP-VER": version,
                "PLATFORM-ID": platform_id,
                "CHANNEL-ID": channel_id
            }
            downloader = FileDownloader(server_url, headers=headers)
            data = downloader.get_response().json()
            latest_catalog_url = data.get("AddressablesCatalogUrlRoots")[0]
            resource_version = data.get("ResourceVersion")
            table_version = data.get("TableVersion")
            media_version = data.get("MediaVersion")
            patch_version = data.get("PatchVersion")
            return latest_catalog_url, resource_version, table_version, media_version, patch_version

    def get_auth_header(self, data: str = "", version: str = "") -> str:
        head = {
            "game_tag": "BlueArchive_JP",
            "time": int(time.time()),
            "version": version,
        }
        sign_str = f"{json.dumps(head, separators=(',', ':'), ensure_ascii=False)}{data or ''}DE7108E9B2842FD460F4777702727869"
        sign = hashlib.md5(sign_str.encode("utf-8")).hexdigest()
        return json.dumps({
            "head": head,
            "sign": sign,
        }, separators=(",", ":"), ensure_ascii=False)

    def get_game_launcher_config(self, version):
        api_headers = {
            "Authorization": self.get_auth_header(version=version)
        }
        url = "https://api-launcher-jp.yo-star.com/api/launcher/game/config"
        response = FileDownloader(url=url, headers=api_headers).get_response()

        if response and response.status_code == 200:
            data = response.json().get("data", {})
            game_latest_version = data.get("game_latest_version")
            game_latest_file_path = data.get("game_latest_file_path")
            file_name = os.path.basename(game_latest_file_path)
            resource_version = os.path.splitext(file_name)[0]
            return game_latest_version, game_latest_file_path, resource_version
        return None, None, None

    def get_zip_config_url(self, version, latest_version, file_path):
        api_headers = {
            "Authorization": self.get_auth_header(version=version)
        }
        params = {
            "version": latest_version,
            "file_path": file_path
        }
        url = "https://api-launcher-jp.yo-star.com/api/launcher/game/config/json"
        response = FileDownloader(
            url=url,
            headers=api_headers,
            params=params
        ).get_response()

        if response and response.status_code == 200:
            return response.json().get("data", {}).get("url")
        return None

    def download_launcher_assets(self, res_version, zip_config_url, targets, dest_dir):
        os.makedirs(dest_dir, exist_ok=True)
        local_json_name = zip_config_url.split("/")[-1]
        downloader = FileDownloader(url=zip_config_url)

        if downloader.save_file(local_json_name):
            with open(local_json_name, 'r', encoding='utf-8') as f:
                config_content = json.load(f)

            base_download_url = "https://launcher-pkg-ba-jp.yo-star.com"

            for file_info in config_content.get("file", []):
                file_path = file_info.get("path", "")
                file_name = os.path.basename(file_path)

                if file_name in targets:
                    full_url = f"{base_download_url}/{res_version}{file_path}"
                    local_save_path = os.path.join(dest_dir, file_name)
                    notice(f"正在从 Launcher 下载资源: {file_name}")
                    dl = FileDownloader(url=full_url, enable_progress=True)
                    dl.save_file(local_save_path)
            return True
        return False

    def get_version_name(self, is_full_name: bool = None, target_version_key: str = None) -> str:
        load_dotenv(Config.env_file.format(server=self.server))
        GameVersion = os.getenv("GameVersion")
        AddressableCatalogUrl = os.getenv("AddressableCatalogUrl")

        if is_full_name:
            if self.server == "CN":
                if target_version_key:
                    version = os.getenv(target_version_key)
                    return f"{self.server}{GameVersion}({version})"
            else:
                version = AddressableCatalogUrl.rsplit("/", 1)[-1]
                return f"{self.server}{GameVersion}({version})"

        return f"{self.server}{GameVersion}"
