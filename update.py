import os
import shutil
import json
import time

from dotenv import load_dotenv
from argparse import ArgumentParser
from datetime import datetime
from zoneinfo import ZoneInfo

from utils.gmail import GmailSMTP
from utils.regions import Server
from utils.config import Config
from utils.util import FileUtils, IL2CppDumper, ZipUtils
from utils.cloudflare import CF
from utils.git import Git
from build.build_update import BuildUpdater


class Updater:
    def __init__(self, server_name):
        self.server_name = server_name
        self.mail = GmailSMTP()
        self.server = Server(server_name)
        self.git = Git()
        self.major = False
        self.major_pc = False
        self.version = None

    def run(self):
        start_time = time.time()
        timeout = 5 * 60 * 60

        while time.time() - start_time < timeout:
            try:
                print(f"开始检查 {self.server_name} 更新。")

                changed, self.major, self.major_pc, self.version = self.run_update()

                if changed:
                    self.process_update()
                    break

                print("没有检测到更新，30秒后再次检查。")

            except Exception as e:
                print(f"更新检查失败: {e}")

            if time.time() - start_time >= timeout:
                break

            time.sleep(30)

        print("检查结束，程序退出。")

    def run_update(self):
        env_file = Config.env_file.format(server=self.server_name)
        load_dotenv(env_file, override=True)

        local_version = os.getenv("GameVersion")
        cached_server_url = os.getenv("ServerInfoDataUrl")
        cached_platform_id = os.getenv("PlatformID")
        cached_channel_id = os.getenv("ChannelID")
        local_latest_version = os.getenv("LatestVersion")

        apk_url, version = self.server.get_apk_url()

        major = local_version != version
        major_pc = False

        cf = CF(
            account_id=os.environ["CF_ACCOUNT_ID"],
            api_token=os.environ["CF_API_TOKEN"],
            kv_namespace_id="1f56e1bf592a4ea18d18b2237cdf822d"
        )

        if major:
            print(f"检测到GameVersion大版本更新: {local_version} -> {version}")

            if self.server_name == "JP":
                cf.kv.put(
                    "APK_Official",
                    {
                        "officialVersion": version,
                        "officialUpdateTime": datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
                    }
                )

            if self.server_name == "JPiOS":
                cf.kv.put(
                    "IPA_Official",
                    {
                        "officialVersion": version,
                        "officialUpdateTime": datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
                    }
                )
                print("KV更新成功")

            self.server.main(apk_url)
        else:
            print(f"GameVersion大版本一致 ({version})，尝试增量检查。")

        if self.server_name == "JPPC":
            latest_ver, file_path, res_ver = self.server.get_game_launcher_config(version)
            zip_url = self.server.get_zip_config_url(version, latest_ver, file_path)
            zip_c = zip_url.split("/", 3)[-1]
            major_pc = local_latest_version != latest_ver

            if major_pc:
                print(f"检测到LatestVersion大版本更新: {local_latest_version} -> {latest_ver}")

                cf.kv.put(
                    "Windows_Official",
                    {
                        "officialVersion": latest_ver,
                        "officialUpdateTime": datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
                    }
                )

                print("KV更新成功")
                self.server.download_launcher_assets(
                    res_ver,
                    zip_url,
                    ["resources.assets", "resources.assets.resS"],
                    "Temp/BlueArchive_Data"
                )
            else:
                print(f"LatestVersion大版本一致 ({latest_ver})，尝试增量检查。")

        if self.server_name == "GL" or major or major_pc:
            server_url, platform_id, channel_id = self.server.get_server_url(version)
        else:
            server_url, platform_id, channel_id = cached_server_url, cached_platform_id, cached_channel_id

        addressable_url, res_v, tab_v, med_v, pat_v = self.server.get_addressable_catalog_url(
            server_url,
            platform_id,
            channel_id,
            version
        )

        new_env_content = [
            f"ServerInfoDataUrl={server_url}\n",
            f"AddressableCatalogUrl={addressable_url}\n",
            f"GameVersion={version}\n"
        ]

        if self.server_name == "CN":
            new_env_content.extend([
                f"PlatformID={platform_id}\n",
                f"ChannelID={channel_id}\n",
                f"ResourceVersion={res_v}\n",
                f"TableVersion={tab_v}\n",
                f"MediaVersion={med_v}\n",
                f"PatchVersion={pat_v}\n"
            ])

        if self.server_name == "JPPC":
            new_env_content.extend([
                f"LatestVersion={latest_ver}\n",
                f"FilePath={file_path}\n",
                f"ResourceVersion={res_ver}\n",
                f"ZipConfig={zip_c}\n",
                f"ZipConfigUrl={zip_url}\n"
            ])

        with open(env_file, "r", encoding="utf-8") as f:
            old_lines = f.readlines()

        env_changed = old_lines != new_env_content

        if env_changed:
            with open(env_file, "w", encoding="utf-8") as f:
                f.writelines(new_env_content)

            load_dotenv(env_file, override=True)
            print(f"{env_file} 配置同步完成。")

            if self.server_name == "JP":
                for table_name in ["Table_Official", "CNVoice_Official", "KRVoice_Official", "Media_Official"]:
                    cf.kv.put(
                        table_name,
                        {
                            "officialVersion": version,
                            "officialUpdateTime": datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
                        }
                    )
                print("KV更新成功")
        else:
            print(f"{env_file} 配置无更新。")

        if major and self.server_name in ("JP", "GL", "CN"):
            self.dump_il2cpp()

        return env_changed, major, major_pc, version

    def dump_il2cpp(self):
        dumper = IL2CppDumper(install_dir="tools")

        metadata_path = os.path.abspath(
            FileUtils.find_files(
                "Temp",
                [r"global-metadata\.dat"],
                True,
                True
            )[0]
        )

        dumper.dump_il2cpp(
            "cn",
            "",
            metadata_path,
            os.path.abspath("Dumps/dump.cs")
        )

        print("成功生成dump.cs。")

        dumper.compile_python(
            os.path.join(os.path.abspath("Dumps"), "dump.cs"),
            "FlatData"
        )

        print("成功生成FlatData库。")
        shutil.rmtree("Temp")

    def commit_config(self):
        print("检测到配置发生变化，开始提交 Git。")

        self.git.pull()
        self.git.add(Config.env_file.format(server=self.server_name))
        self.git.commit(f"{self.server_name}服务器变动，提交配置。")
        self.git.push()

        print("Git配置提交完成。")

    def send_update_notice(self):
        if os.getenv("GITHUB_RUN_ID"):
            self.mail.send_update_notice(
                server_name=self.server_name,
                run_id=os.getenv("GITHUB_RUN_ID")
            )

    def upload_flatdata(self):
        if self.server_name not in ("JP", "GL", "CN"):
            return

        version_name = self.server.get_version_name()
        print(f"当前FlatData版本名称: {version_name}")

        zip_path = f"{version_name}.zip"

        ZipUtils.create_zip(["Dumps", "FlatData"], zip_path)

        print(f"FlatData打包完成: {zip_path}")

        flatdata_dir = "BA-FlatData"

        if not os.path.exists(flatdata_dir):
            print("未找到BA-FlatData目录，开始克隆仓库。")
            self.git.clone(Config.FlatData_repositories, flatdata_dir)

        flatdata_git = Git(flatdata_dir)

        flatdata_git.checkout("main")
        flatdata_git.pull("main")

        shutil.move(zip_path, os.path.join(flatdata_dir, zip_path))

        flatdata_git.add(zip_path)
        flatdata_git.commit(f"上传{version_name}.zip")
        flatdata_git.push("main")

        print(f"ZIP上传完成: {zip_path}")

        flatdata_git.fetch(self.server_name)
        flatdata_git.checkout(self.server_name)
        flatdata_git.pull(self.server_name)

        flatdata_source = os.path.abspath("FlatData")

        for item in os.listdir(flatdata_source):
            src = os.path.join(flatdata_source, item)
            dst = os.path.join(flatdata_dir, item)

            if os.path.isdir(src):
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dst)

        flatdata_git.add(".")

        if flatdata_git.has_changes():
            flatdata_git.commit(f"提交FlatData，版本{version_name}")
            flatdata_git.push(self.server_name)
            print(f"{self.server_name}分支FlatData上传完成。")
        else:
            print("没有检测到FlatData变动，跳过提交。")

    def dispatch_resource_events(self):
        if self.server_name not in ("JP", "GL", "CN"):
            return

        types = ["Extract"]

        if self.server_name in ("GL", "CN"):
            types.append("Voice")

        if self.server_name == "JP":
            types.append("Build")

        for event_type in types:
            resource_types = ["Table", "Media", "Bundle"] if event_type == "Extract" and self.server_name == "JP" else ["Table"]

            for resource_type in resource_types:
                payload = {
                    "server": self.server_name,
                    "type": resource_type
                }
                self.git.dispatch(event_type, payload)

    def sync_pc_source(self):
        if self.server_name != "JPPC":
            return

        pc_src_dir = "BA-PC-SRC"
        pc_src_git = Git(pc_src_dir)

        pc_src_git.init()
        pc_src_git.add_remote("origin", Config.PC_repositories)
        pc_src_git.fetch("main")
        pc_src_git.checkout("main")
        pc_src_git.add(".")

        if pc_src_git.has_staged_changes():
            pc_src_git.commit("提交BA-PC-SRC")
            pc_src_git.push("main")
            print("BA-PC-SRC main分支上传完成。")
        else:
            print("BA-PC-SRC没有检测到变动，跳过提交。")

        pc_install_dir = "BA-PC-SRC-INSTALL"
        pc_install_git = Git(pc_install_dir)

        pc_install_git.init()
        pc_install_git.add_remote("origin", Config.PC_repositories)
        pc_install_git.fetch("install")
        pc_install_git.checkout("install")
        pc_install_git.add(".")

        if pc_install_git.has_staged_changes():
            pc_install_git.commit("提交BA-PC-SRC-INSTALL")
            pc_install_git.push("install")
            print("BA-PC-SRC install分支上传完成。")
        else:
            print("BA-PC-SRC-INSTALL没有检测到变动，跳过提交。")

    def build_apk(self):
        if not (self.major or self.major_pc) or self.server_name not in ("JP", "JPiOS", "JPPC"):
            return

        env_file = Config.env_file.format(server=self.server_name)

        load_dotenv(env_file, override=True)

        server_info_url = os.getenv("ServerInfoDataUrl")

        modified_url = server_info_url.replace(
            "bluearchiveyostar.com",
            "bluearchive.help"
        )

        print(f"修改后ServerInfoDataUrl: {modified_url}")

        apk_src_dir = "BA-APKSRC"

        if not os.path.exists(apk_src_dir):
            print("未找到BA-APKSRC目录，开始克隆仓库。")
            self.git.clone(Config.APK_repositories, apk_src_dir)

        updater = BuildUpdater(
            repo=apk_src_dir,
            server=self.server_name,
            workers=1
        )

        updater.run(
            sdkurl="https://jp-sdk-api.bluearchive.help/",
            gamemainconfig=json.dumps(
                {"ServerInfoDataUrl": modified_url},
                separators=(",", ":")
            ),
            trustcert=True,
            modifylogin=True,
            modifygt4="zho",
            replace=True,
            modifybundle=True,
            upload=True
        )

    def process_update(self):
        self.commit_config()
        self.send_update_notice()

        if self.major and self.server_name in ("JP", "GL", "CN"):
            self.upload_flatdata()

        self.dispatch_resource_events()

        if self.major and self.server_name == "JPPC":
            self.sync_pc_source()

        if (self.major or self.major_pc) and self.server_name in ("JP", "JPiOS", "JPPC"):
            self.build_apk()

        print("Git提交完成，程序退出。")


if __name__ == "__main__":
    parser = ArgumentParser(description="BA资源自动更新")
    parser.add_argument("server", choices=["JP", "JPPC", "JPiOS", "GL", "GLiOS", "CN"], help="选择服务器区域")
    args = parser.parse_args()

    Updater(args.server).run()
