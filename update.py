import os
import shutil
import json
import time
import subprocess
from dotenv import load_dotenv
from utils.regions import Server
from utils.config import Config
from utils.util import FileUtils, IL2CppDumper, ZipUtils
from utils.console import notice
from utils.cloudflare import CF
from utils.server import SSHServer
from utils.git import Git
from build.build_apk import ApkUpdater
from argparse import ArgumentParser
from datetime import datetime
from zoneinfo import ZoneInfo

def run_update(regions, server):
    env_file = Config.env_file.format(server=regions)
    load_dotenv(env_file)
    
    local_version = os.getenv("GameVersion")
    cached_server_url = os.getenv("ServerInfoDataUrl")
    cached_platform_id = os.getenv("PlatformID")
    cached_channel_id = os.getenv("ChannelID")
    local_latest_version = os.getenv("LatestVersion")

    apk_url, version = server.get_apk_url()
    
    major = local_version != version
    major_pc = False

    cf = CF(
        account_id=os.environ["CF_ACCOUNT_ID"],
        api_token=os.environ["CF_API_TOKEN"],
        kv_namespace_id="1f56e1bf592a4ea18d18b2237cdf822d"
    )

    # GameVersion大版本更新
    if major:
        notice(f"检测到GameVersion大版本更新: {local_version} -> {version}")

        if regions == "JP":
            cf.kv.put(
                "APK_Official",
                {
                    "officialVersion": version,
                    "officialUpdateTime": datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
                }
            )

            print("KV更新成功")
        server.main(apk_url, version)
    else:
        notice(f"GameVersion大版本一致 ({version})，尝试增量检查。")

    if regions == "JPPC":
        latest_ver, file_path, res_ver = server.get_game_launcher_config(version)
        zip_url = server.get_zip_config_url(version, latest_ver, file_path)
        major_pc = local_latest_version != latest_ver
        # LatestVersion大版本更新
        if major_pc:
            notice(f"检测到LatestVersion大版本更新: {local_latest_version} -> {latest_ver}")
            cf.kv.put(
                "Windows_Official",
                {
                    "officialVersion": latest_ver,
                    "officialUpdateTime": datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
                }
            )

            print("KV更新成功")
            server.download_launcher_assets(res_ver, zip_url, ["resources.assets", "resources.assets.resS"], "Temp/assets/bin/Data")
        else:
            notice(f"LatestVersion大版本一致 ({latest_ver})，尝试增量检查。")

    if regions == "GL":
        server_url, platform_id, channel_id = server.get_server_url(version)
    else:
        if major or major_pc:
            server_url, platform_id, channel_id = server.get_server_url(version)
        else:
            server_url, platform_id, channel_id = cached_server_url, cached_platform_id, cached_channel_id

    # 获取 Addressable 等详细信息
    addressable_url, res_v, tab_v, med_v, pat_v = server.get_addressable_catalog_url(
        server_url, platform_id, channel_id, version
    )

    # 写入环境变量
    new_env_content = [
        f"ServerInfoDataUrl={server_url}\n",
        f"AddressableCatalogUrl={addressable_url}\n",
        f"GameVersion={version}\n"
    ]

    # 追加 CN 特有字段
    if regions == "CN":
        new_env_content.extend([
            f"PlatformID={platform_id}\n",
            f"ChannelID={channel_id}\n",
            f"ResourceVersion={res_v}\n",
            f"TableVersion={tab_v}\n",
            f"MediaVersion={med_v}\n",
            f"PatchVersion={pat_v}\n"
        ])
    
    # 追加 JPPC 特有字段
    if regions == "JPPC":
        new_env_content.extend([
            f"LatestVersion={latest_ver}\n",
            f"FilePath={file_path}\n",
            f"ResourceVersion={res_ver}\n",
            f"ZipConfigUrl={zip_url}\n"
        ])

    # 检查并更新 env 文件
    with open(env_file, "r", encoding="utf-8") as f:
        old_lines = f.readlines()

    env_changed = old_lines != new_env_content

    if env_changed:
        with open(env_file, "w", encoding="utf-8") as f:
            f.writelines(new_env_content)
        notice(f"{env_file} 配置同步完成。")

        if regions == "JP":
            for table_name in ["Table_Official", "Voice_Official", "Media_Official"]:
                cf.kv.put(
                    table_name,
                    {
                        "officialVersion": version,
                        "officialUpdateTime": datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
                    }
                )
            print("KV更新成功")
    else:
        notice(f"{env_file} 配置无更新。")

    # 后续 Dumper 逻辑（非 JPPC 且是大版本时运行）
    if major and regions != "JPPC":
        # 直接metadata扫，不再使用il2cpp了
        dumper = IL2CppDumper(install_dir="tools")

        metadata_path = os.path.abspath(FileUtils.find_files("Temp", [r"global-metadata\.dat"], True, True)[0])
        # il2cpp已被压缩加密，因此不再寻找，解密方法需要hook获得双加密AES密钥
        # il2cpp_path = os.path.abspath(FileUtils.find_files("Temp", [r"libil2cpp\.so"], True, True)[0])
        # 这里直接用cn的方式，fl已适配
        dumper.dump_il2cpp("cn", "", metadata_path, os.path.abspath("Dumps/dump.cs"))

        notice("成功生成dump.cs。")
        dumper.compile_python(os.path.join(os.path.abspath("Dumps"), "dump.cs"), "FlatData")
        notice("成功生成FlatData库。")
        shutil.rmtree("Temp")
    return env_changed, major


def main():
    parser = ArgumentParser(description="BA资源自动更新")
    parser.add_argument(
        "server",
        choices=["JP", "JPPC", "GL", "CN"],
        help="选择服务器区域"
    )
    args = parser.parse_args()

    server = Server(args.server)
    git = Git()

    start_time = time.time()
    timeout = 5 * 60 * 60

    while time.time() - start_time < timeout:
        try:
            notice(f"开始检查 {args.server} 更新。")

            changed, major = run_update(args.server, server)

            if changed:
                notice("检测到配置发生变化，开始提交 Git。")

                git.pull()
                git.add(Config.env_file.format(server=args.server))
                git.commit(f"{args.server}服务器变动，提交配置。")
                git.push()

                if major:
                    if args.server != "JPPC":
                        version_name = server.get_version_name()
                        notice(f"当前FlatData版本名称: {version_name}")

                        zip_path = f"{version_name}.zip"
                        ZipUtils.create_zip(
                            ["Dumps", "FlatData"],
                            zip_path,
                            progress_bar=True
                        )
                        notice(f"FlatData打包完成: {zip_path}")

                        flatdata_dir = "BA-FlatData"

                        if not os.path.exists(flatdata_dir):
                            notice("未找到BA-FlatData目录，开始克隆仓库。")
                            git.clone(Config.FlatData, flatdata_dir)

                        flatdata_git = Git(flatdata_dir)

                        # 上传版本ZIP到main
                        flatdata_git.checkout("main")
                        flatdata_git.pull("main")

                        shutil.move(
                            zip_path,
                            os.path.join(flatdata_dir, zip_path)
                        )

                        flatdata_git.add(zip_path)
                        flatdata_git.commit(f"上传{version_name}.zip")
                        flatdata_git.push("main")

                        notice(f"ZIP上传完成: {zip_path}")

                        # 上传FlatData到服务器分支
                        flatdata_git.fetch(args.server)
                        flatdata_git.checkout(args.server)
                        flatdata_git.pull(args.server)

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
                            flatdata_git.commit(
                                f"提交FlatData，版本{version_name}"
                            )
                            flatdata_git.push(args.server)
                            notice(f"{args.server}分支FlatData上传完成。")
                        else:
                            notice("没有检测到FlatData变动，跳过提交。")


                        if args.server == "JP":
                            notice("大版本更新且为 JP，密钥已变动，触发 JP APK 部署。")

                            env_file = Config.env_file.format(server=args.server)

                            load_dotenv(env_file)
                            server_info_url = os.getenv("ServerInfoDataUrl")

                            modified_url = server_info_url.replace(
                                "bluearchiveyostar.com",
                                "bluearchive.help"
                            )

                            notice(f"修改后ServerInfoDataUrl: {modified_url}")

                            # 检查 BA-APKSRC 仓库是否存在
                            apk_src_dir = "BA-APKSRC"

                            if not os.path.exists(apk_src_dir):
                                notice("未找到BA-APKSRC目录，开始克隆仓库。")
                                git.clone(Config.apk_src, apk_src_dir)

                            updater = ApkUpdater(
                                repo=apk_src_dir,
                                server=args.server,
                                workers=4,
                            )

                            updater.run(
                                sdkurl="https://jp-sdk-api.bluearchive.help/",
                                gamemainconfig=json.dumps(
                                    {
                                        "ServerInfoDataUrl": modified_url
                                    },
                                    separators=(",", ":")
                                ),
                                trustcert=True,
                                modifylogin=True,
                                modifygt4="zho",
                                replace=True,
                                modifybundle=True,
                            )

                            server = SSHServer(
                                host=os.environ["SERVER_HOST"],
                                username="root",
                                password=os.environ["SERVER_PASSWORD"],
                                port=22
                            )

                            remote_directory = "/var/www/web_download"

                            print("正在检查服务器连接...")

                            if not server.test_connection():
                                raise RuntimeError("服务器连接失败")

                            print("服务器连接成功")

                            print("正在检查远程目录...")

                            if not server.is_dir(remote_directory):
                                print("web_download 文件夹不存在，正在创建...")

                                server.mkdir(
                                    remote_directory,
                                    parents=True
                                )

                                print("web_download 文件夹创建成功")
                            else:
                                print("web_download 文件夹已存在")

                            print("开始上传Android客户端...")

                            server.upload_file(
                                str(final_apk),
                                "/var/www/web_download/蔚蓝档案.apk",
                                create_parent=False
                            )

                            print("上传完成")

                            cf = CF(
                                account_id=os.environ["CF_ACCOUNT_ID"],
                                api_token=os.environ["CF_API_TOKEN"],
                                kv_namespace_id="1f56e1bf592a4ea18d18b2237cdf822d"
                            )

                            cf.kv.put(
                                "APK_Resource",
                                {
                                    "resourceVersion": version,
                                    "resourceUpdateTime": datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
                                }
                            )

                            print("KV更新成功")

                            # BuildAPK GitHub Actions 调用
                            # git.dispatch(
                            #     "BuildAPK",
                            #     {
                            #         "server": "JP",
                            #         "sdkurl": "https://jp-sdk-api.bluearchive.help/",
                            #         "gamemainconfig": json.dumps(
                            #             {
                            #                 "ServerInfoDataUrl": modified_url
                            #             },
                            #             separators=(",", ":")
                            #         ),
                            #         "modifylogin": True,
                            #         "modifygt4": "zho",
                            #         "replace": True,
                            #         "modifybundle": True,
                            #         "repo_url": "https://github.com/BlueArchive-Translation/BA-APKSRC.git",
                            #         "repo": "BA-APKSRC",
                            #         "trustcert": True,
                            #         "upload": True
                            #     }
                            # )
                    else:
                        pc_src_dir = "BA-PC-SRC"
                        pc_src_git = Git(pc_src_dir)

                        pc_src_git.init()

                        pc_src = Config.pc_src

                        origin_url = pc_src_git.get_remote_url()

                        if origin_url is None:
                            pc_src_git.add_remote("origin", pc_src)
                        elif origin_url != pc_src:
                            pc_src_git.set_remote_url("origin", pc_src)

                        pc_src_git.add(".")
                    
                        if pc_src_git.has_staged_changes():
                            pc_src_git.commit("提交BA-PC-SRC")
                            pc_src_git.push("main", set_upstream=True)
                            notice("BA-PC-SRC上传完成。")
                        else:
                            notice("BA-PC-SRC没有检测到变动，跳过提交。")

                if args.server != "JPPC":
#                    types = ["Table"]
                    types = []
#                    if args.server in ("GL", "CN"):
#                        types.append("Voice")
#                    if args.server == "JP":
#                        types.append("RepackTable")

                    for event_type in types:
                        payload = {
                            "server": args.server,
                            "platform": "auto",
                            "modify_name": "false",
                            "debug": "false",
                            "voice_lang": "Default",
                            "catalog": "true",
                            "upload": "true"
                        }
                        git.dispatch(event_type, payload)

                notice("Git提交完成，程序退出。")
                break

            notice("没有检测到更新，30秒后再次检查。")

        except Exception as e:
            notice(f"更新检查失败: {e}")

        if time.time() - start_time >= timeout:
            break

        time.sleep(30)

    notice("检查结束，程序退出。")


if __name__ == "__main__":
    main()
