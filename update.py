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

def run_update(regions, server):
    env_file = Config.env_file.format(server=regions)
    load_dotenv(env_file, override=True)

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
        print(f"检测到GameVersion大版本更新: {local_version} -> {version}")

        if regions == "JP":
            cf.kv.put(
                "APK_Official",
                {
                    "officialVersion": version,
                    "officialUpdateTime": datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
                }
            )

        if regions == "JPiOS":
            cf.kv.put(
                "IPA_Official",
                {
                    "officialVersion": version,
                    "officialUpdateTime": datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
                }
            )
            print("KV更新成功")

        server.main(apk_url)
    else:
        print(f"GameVersion大版本一致 ({version})，尝试增量检查。")

    if regions == "JPPC":
        latest_ver, file_path, res_ver = server.get_game_launcher_config(version)
        zip_url = server.get_zip_config_url(version, latest_ver, file_path)
        zip_c = zip_url.split('/', 3)[-1]
        major_pc = local_latest_version != latest_ver

        # LatestVersion大版本更新
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
            server.download_launcher_assets(res_ver, zip_url, ["resources.assets", "resources.assets.resS"], "Temp/BlueArchive_Data")
        else:
            print(f"LatestVersion大版本一致 ({latest_ver})，尝试增量检查。")

    if regions == "GL" or major or major_pc:
        server_url, platform_id, channel_id = server.get_server_url(version)
    else:
        server_url, platform_id, channel_id = cached_server_url, cached_platform_id, cached_channel_id

    # 获取 Addressable 等详细信息
    addressable_url, res_v, tab_v, med_v, pat_v = server.get_addressable_catalog_url(server_url, platform_id, channel_id, version)

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
            f"ZipConfig={zip_c}\n",
            f"ZipConfigUrl={zip_url}\n"
        ])

    # 检查并更新 env 文件
    with open(env_file, "r", encoding="utf-8") as f:
        old_lines = f.readlines()

    env_changed = old_lines != new_env_content

    if env_changed:
        with open(env_file, "w", encoding="utf-8") as f:
            f.writelines(new_env_content)

        load_dotenv(env_file, override=True)
        print(f"{env_file} 配置同步完成。")

        if regions == "JP":
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

    # 后续 Dumper 逻辑（JP/GL/CN且是大版本时运行）
    if major and regions in ("JP", "GL", "CN"):
        dumper = IL2CppDumper(install_dir="tools")

        metadata_path = os.path.abspath(FileUtils.find_files( "Temp", [r"global-metadata\.dat"], True, True)[0])

        # il2cpp已被压缩加密，因此不再寻找
        # il2cpp_path = os.path.abspath(FileUtils.find_files("Temp", [r"libil2cpp\.so"], True, True)[0])
        # 这里直接用cn的方式，fl已适配
        dumper.dump_il2cpp("cn", "", metadata_path, os.path.abspath("Dumps/dump.cs"))

        print("成功生成dump.cs。")

        dumper.compile_python(os.path.join(os.path.abspath("Dumps"), "dump.cs"), "FlatData")

        print("成功生成FlatData库。")
        shutil.rmtree("Temp")

    return env_changed, major, major_pc, version


def commit_config(git, server_name):
    print("检测到配置发生变化，开始提交 Git。")

    git.pull()
    git.add(Config.env_file.format(server=server_name))
    git.commit(f"{server_name}服务器变动，提交配置。")
    git.push()

    print("Git配置提交完成。")


def send_update_notice(mail, server_name):
    if os.getenv("GITHUB_RUN_ID"):
        mail.send_update_notice(server=server_name, run_id=os.getenv("GITHUB_RUN_ID"))

def upload_flatdata(git, server, server_name, version):
    if server_name not in ("JP", "GL", "CN"):
        return

    version_name = server.get_version_name()
    print(f"当前FlatData版本名称: {version_name}")

    zip_path = f"{version_name}.zip"

    ZipUtils.create_zip(["Dumps", "FlatData"], zip_path)

    print(f"FlatData打包完成: {zip_path}")

    flatdata_dir = "BA-FlatData"

    if not os.path.exists(flatdata_dir):
        print("未找到BA-FlatData目录，开始克隆仓库。")
        git.clone(Config.FlatData_repositories, flatdata_dir)

    flatdata_git = Git(flatdata_dir)

    # 上传版本ZIP到main
    flatdata_git.checkout("main")
    flatdata_git.pull("main")

    shutil.move(zip_path, os.path.join(flatdata_dir, zip_path))

    flatdata_git.add(zip_path)
    flatdata_git.commit(f"上传{version_name}.zip")
    flatdata_git.push("main")

    print(f"ZIP上传完成: {zip_path}")

    # 上传FlatData到服务器分支
    flatdata_git.fetch(server_name)
    flatdata_git.checkout(server_name)
    flatdata_git.pull(server_name)

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
        flatdata_git.push(server_name)
        print(f"{server_name}分支FlatData上传完成。")
    else:
        print("没有检测到FlatData变动，跳过提交。")


def dispatch_resource_events(git, server_name):
    if server_name not in ("JP", "GL", "CN"):
        return

    types = ["Extract"]

    if server_name in ("GL", "CN"):
        types.append("Voice")

    if server_name == "JP":
        types.append("Repack")

    for event_type in types:
        resource_types = ["Table", "Media", "Bundle"] if event_type == "Extract" else ["Table"]

        for resource_type in resource_types:
            payload = {
                "server": server_name,
                "type": resource_type
            }
            git.dispatch(event_type, payload)


def sync_pc_source(git, server_name):
    if server_name != "JPPC":
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


def build_apk(server_name, major, major_pc):
    if not (major or major_pc) or server_name not in ("JP", "JPiOS", "JPPC"):
        return

    env_file = Config.env_file.format(server=server_name)

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
        git.clone(Config.APK_repositories, apk_src_dir)

    updater = BuildUpdater(
        repo=apk_src_dir,
        server=server_name,
        workers=1,
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
        upload=True,
    )


def process_update(mail, git, server, server_name, major, major_pc, version):
    commit_config(git, server_name)
    send_update_notice(mail, server_name)

    if major and server_name in ("JP", "GL", "CN"):
        upload_flatdata(git, server, server_name, version)

    dispatch_resource_events(git, server_name)

    # 调换先后顺序，build占用导致请求发送慢了
    if major and server_name == "JPPC":
        sync_pc_source(git, server_name)

    if (major or major_pc) and server_name in ("JP", "JPiOS", "JPPC"):
        build_apk(server_name, major, major_pc)

    print("Git提交完成，程序退出。")


if __name__ == "__main__":
    parser = ArgumentParser(description="BA资源自动更新")
    parser.add_argument("server", choices=["JP", "JPPC", "JPiOS", "GL", "GLiOS", "CN"], help="选择服务器区域")
    args = parser.parse_args()

    mail = GmailSMTP()
    server = Server(args.server)
    git = Git()

    start_time = time.time()
    timeout = 5 * 60 * 60

    while time.time() - start_time < timeout:
        try:
            print(f"开始检查 {args.server} 更新。")

            changed, major, major_pc, version = run_update(args.server, server)

            if changed:
                process_update(
                    mail,
                    git,
                    server,
                    args.server,
                    major,
                    major_pc,
                    version
                )
                break

            print("没有检测到更新，30秒后再次检查。")

        except Exception as e:
            print(f"更新检查失败: {e}")

        if time.time() - start_time >= timeout:
            break

        time.sleep(30)

    print("检查结束，程序退出。")
