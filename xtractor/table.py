import importlib
import json
import os
import shutil
import tempfile
import time
import flatbuffers
import secrets

from os import path
from pathlib import Path
from types import ModuleType
from typing import Any, Union
from zipfile import ZipFile, ZIP_DEFLATED

from utils.console import notice
from utils.encryption import xor_with_key, zip_password
from utils.structure import DBTable, SQLiteDataType
from utils.database import TableDatabase
from utils.config import Config
from utils.util import ZipUtils
from utils.download import ResourceDownloader
from utils.git import Git
from utils.regions import Server

class TableProcess:
    def __init__(
        self, server: str = "JP", password: str = "", table_file_folder: str = "", extract_folder: str = "", flat_data_module_name: str = "FlatData"
    ) -> None:
        """Extract files in table folder.

        Args:
            table_file_folder (str): Folder own table files.
            extract_folder (str): Folder to store the extracted/repack data.
            flat_data_module_name (str): Name path to import flat data module. Most like "Extracted.FlatData".
        """
        self.table_file_folder = table_file_folder
        self.extract_folder = extract_folder
        self.server = server
        self.password = password
        self.flat_data_module_name = flat_data_module_name

        self.lower_fb_name_modules: dict[str, type] = {}
        self.dump_wrapper_lib: ModuleType
        self.repack_wrapper_lib: ModuleType

        self.__import_modules()

    def __import_modules(self):
        try:
            flat_data_lib = importlib.import_module(self.flat_data_module_name)
            self.dump_wrapper_lib = importlib.import_module(
                f"{self.flat_data_module_name}.dump_wrapper"
            )
            self.repack_wrapper_lib = importlib.import_module(
                f"{self.flat_data_module_name}.repack_wrapper"
            )
            self.lower_fb_name_modules = {
                t_name.lower(): t_class
                for t_name, t_class in flat_data_lib.__dict__.items()
            }
        except Exception as e:
            notice(
                f"Cannot import FlatData module. Make sure FlatData is available in Extracted folder. {e}",
                "error",
            )

    def _process_bytes_file(
        self, file_name: str, data: bytes
    ) -> tuple[dict[str, Any], str]:
        """Extract flatbuffer bytes file to dict

        Args:
            file_name (str): Schema name of data.
            data (bytes): Flatbuffer data to extract.

        Returns:
            tuple[dict[str, Any], str]: Tuple with extracted dict and file name. Always have file name if success extract.
        """
        if not (
            flatbuffer_class := self.lower_fb_name_modules.get(
                file_name.removesuffix(".bytes").lower(), None
            )
        ):
            return {}, ""

        obj = None
        try:
            if flatbuffer_class.__name__.endswith("Table"):
                try:
                    if not file_name.endswith(".bytes") or self.server != "CN":
                        data = xor_with_key(flatbuffer_class.__name__, data)
                    flat_buffer = getattr(flatbuffer_class, "GetRootAs")(data)
                    obj = getattr(self.dump_wrapper_lib, "dump_table")(flat_buffer)
                except:
                    pass

            if not obj:
                flat_buffer = getattr(flatbuffer_class, "GetRootAs")(data)
                obj = getattr(
                    self.dump_wrapper_lib, f"dump_{flatbuffer_class.__name__}"
                )(flat_buffer)
            return (obj, f"{flatbuffer_class.__name__}.json")
        except:
            return {}, ""

    def _repack_bytes_file(
        self, file_name: str, json_data: Union[dict, list], encrypt: bool = True, xor_encrypt: bool = True
    ) -> tuple[bytes, str]:
        """Repack dict to encrypted flatbuffer bytes

        Args:
            file_name (str): File name of json.
            json_data (dict | list): Content to repack.

        Returns:
            tuple[bytes, str]: Tuple with encrypted bytes and original bytes file name.
        """
        base_name = file_name.removesuffix(".json").lower()
        if not (
            flatbuffer_class := self.lower_fb_name_modules.get(base_name, None)
        ):
            return b"", ""

        try:
            # 逻辑如下
            # 先使用pack_{class_name} 进行序列化（序列化后需要进行xor字段加密，repack_wrapper已写应对方式），xor密钥为字段名
            # 随后进行xor加密（密钥为FlatData表名）
            class_name = flatbuffer_class.__name__
            pack_func_name = f"pack_{class_name}"
            pack_func = getattr(self.repack_wrapper_lib, pack_func_name, None)

            if not pack_func:
                return b"", ""

            builder = flatbuffers.Builder(4096)
            offset = pack_func(builder, json_data, encrypt)
            builder.Finish(offset)
            bytes_output = bytes(builder.Output())

            # 与解压同流程加密
            if not (file_name.endswith(".bytes") and self.server == "CN") and xor_encrypt:
                bytes_output = xor_with_key(class_name, bytes_output)

            return bytes_output, f"{base_name}.bytes"
        except:
            return b"", ""

    def _process_json_file(self, data: bytes) -> bytes:
        """Extract json file in zip.

        Args:
            file_name (str): File name. 
            data (bytes): Data of file.

        Returns:
            bytes: Bytes of json data.
        """
        try:
            data.decode("utf8")
            return data
        except:
            return bytes()

    def _process_db_file(self, file_path: str, table_name: str = "") -> list[DBTable]:
        """Extract sqlite database file.

        Args:
            file_path (str): Database path.
            table_name (str): Specify table to extract.

        Returns:
            list[DBTable]: A list of DBTables.
        """
        with TableDatabase(file_path, self.password) as db:
            tables = []

            table_list = [table_name] if table_name else db.get_table_list()

            for table in table_list:
                columns = db.get_table_column_structure(table)
                rows: list[tuple] = db.get_table_data(table)[1]
                table_data = []
                for row in rows:
                    row_data: list[Any] = []
                    for col, value in zip(columns, row):
                        col_type = SQLiteDataType[col.data_type].value
                        if col_type == bytes:
                            data, _ = self._process_bytes_file(
                                table.replace("DBSchema", "Excel"), value
                            )
                            row_data.append(data)
                        elif col_type == bool:
                            row_data.append(bool(value))
                        else:
                            row_data.append(value)

                    table_data.append(row_data)
                tables.append(DBTable(table, columns, table_data))
            return tables

    def _process_zip_file(
        self,
        file_name: str,
        file_data: bytes,
        detect_type: bool = False,
    ) -> tuple[bytes, str, bool]:
        data = bytes()
        if (detect_type or file_name.endswith(".json")) and (
            data := self._process_json_file(file_data)
        ):
            return data, "", True

        if detect_type or file_name.endswith(".bytes"):
            b_data = self._process_bytes_file(file_name, file_data)
            file_dict, file_name = b_data
            if file_name:
                return (
                    json.dumps(file_dict, indent=4, ensure_ascii=False).encode("utf8"),
                    file_name,
                    True,
                )
        return data, "", False


class TableExtract(TableProcess):
    def extract_db_file(self, file_path: str) -> bool:
        """Extract db file."""
        try:
            if db_tables := self._process_db_file(
                path.join(self.table_file_folder, file_path)
            ):
                db_name = file_path.removesuffix(".db")
                for table in db_tables:
                    db_extract_folder = path.join(self.extract_folder, db_name)
                    os.makedirs(db_extract_folder, exist_ok=True)
                    with open(
                        path.join(db_extract_folder, f"{table.name.replace('DBSchema', 'Excel')}.json"),
                        "wt",
                        encoding="utf8",
                    ) as f:
                        json.dump(
                            TableDatabase.convert_to_list_dict(table),
                            f,
                            indent=4,
                            ensure_ascii=False,
                        )
                return True
            return False
        except Exception as e:
            print(f"Error when process {file_path}: {e}")
            return False

    def extract_zip_file(self, file_name: str) -> None:
        """Extract zip file."""
        try:
            zip_extract_folder = path.join(
                self.extract_folder, file_name.removesuffix(".zip")
            )
            os.makedirs(zip_extract_folder, exist_ok=True)

            password = zip_password(path.basename(file_name))
            with ZipFile(path.join(self.table_file_folder, file_name), "r") as zip:
                zip.setpassword(password)
                for item_name in zip.namelist():
                    item_data = zip.read(item_name)

                    data, name, success = bytes(), "", False
                    if item_name.endswith((".json", ".bytes")):
                        if "RootMotion" in file_name:
                            data, name, success = self._process_zip_file(
                                f"{file_name.removesuffix('.zip')}Flat", item_data, True
                            )
                            name = item_name
                        else:
                            data, name, success = self._process_zip_file(
                                item_name, item_data
                            )

                    if not success:
                        data, name, success = self._process_zip_file(
                            item_name, item_data, True
                        )
                    if success:
                        item_name = name if name else item_name
                        item_data = data
                    else:
                        notice(
                            f"The file {item_name} in {file_name} is not be implementate or cannot process."
                        )
                        continue

                    with open(path.join(zip_extract_folder, item_name), "wb") as f:
                        f.write(item_data)
        except Exception as e:
            notice(f"Error when process {file_name}: {e}")


class TableRepack(TableProcess):
    def repack_to_zip(self, file_name: str) -> None:
        """Repack JSON files back to original zip file."""
        try:
            zip_path = path.join(self.table_file_folder, file_name)
            password = zip_password(path.basename(file_name)) if Config.server != "CN" else None

            # 解压到临时目录，extract_zip_file不是我写的懒得改
            os.makedirs("Temp", exist_ok=True)
            ZipUtils.extract_zip(
                zip_path=zip_path,
                dest_dir="Temp",
                password=password,
                progress_bar=False
            )
            # self.extract_folder/Excel路径即为需打包的json路径，Replacement需要大改
            # 检查文件在不在写回目录
            for root, _, files in os.walk(path.join(self.extract_folder, "Excel")):
                for file in files:
                    if file.endswith(".json"):
                        with open(path.join(root, file), 'r', encoding='utf8') as f:
                            json_data = json.load(f)

                        item_data, new_name = self._repack_bytes_file(file, json_data, False)

                        if new_name:
                            # 将修改后的数据写回临时目录以备重新打包
                            target_file_path = path.join("Temp", new_name)
                            with open(target_file_path, "wb") as f:
                                f.write(item_data)

            # 重新打包
            success = ZipUtils.create_zip(
                file_paths=os.listdir("Temp"),
                dest_zip=zip_path,
                base_dir="Temp",
                password=password,
                progress_bar=False
            )

            if success:
                notice(f"Successfully repacked {file_name}")

            shutil.rmtree("Temp", ignore_errors=True)

        except Exception as e:
            notice(f"Error when repack {file_name}: {e}")

    def repack_to_db(self, file_name: str) -> None:
        """Repack JSON files back to original sqlite database."""
        try:
            db_name = file_name.removesuffix(".db")
            db_extract_folder = path.join(self.extract_folder, db_name)

            db_path = path.join(self.table_file_folder, file_name)

            with TableDatabase(db_path, self.password) as db:
                json_files = [f for f in os.listdir(db_extract_folder) if f.endswith(".json")]
                total_files = len(json_files)

                for index, file in enumerate(json_files):
                    table_name = file.removesuffix(".json").replace("Excel", "DBSchema")
                    print(f"[{index + 1}/{total_files}] 正在转换数据表: {table_name} ...", end="\r")

                    with open(path.join(db_extract_folder, file), 'r', encoding='utf8') as f:
                        json_data = json.load(f)

                    columns = db.get_table_column_structure(table_name)
                    column_names = [col.name for col in columns]

                    new_rows = []
                    for item in json_data:
                        row = []
                        for col in columns:
                            if col.name == "Bytes":
                                byte_data, _ = self._repack_bytes_file(file, item, False, False)
                                row.append(byte_data)
                            else:
                                row.append(item.get(col.name))
                        new_rows.append(row)

                    print(f"正在写入数据库 {table_name} ({len(new_rows)} 行)...")
                    db.update_table_data(table_name, column_names, new_rows)
                    print(f"{table_name} 写入完成。")

                print("正在优化数据库文件大小...")
                db.execute("VACUUM")

                notice(f"Successfully repacked {file_name}")
        except Exception as e:
            notice(f"Error when repack {file_name}: {e}", "error")

class TableTask:
    def __init__(self, server, region="na", dispatch_type="Extract"):
        self.server = server
        self.region = region
        self.dispatch_type = dispatch_type

        self.key = None
        self.download = None
        self.server_info = Server(server)

        self.temp_path = None
        self.output_path = None

    def prepare(self):
        self.prepare_api()

        if self.server in ("JP", "GL"):
            self.key = self.get_key()

        self.download = ResourceDownloader(self.server)

        self.temp_path = tempfile.mkdtemp(prefix="Download_")
        self.output_path = tempfile.mkdtemp(prefix="Output_")

        return self

    def prepare_api(self):
        if self.server not in ("JP", "GL"):
            return

        required_dirs = ("lib", "code", "request_api")

        if all(os.path.isdir(name) for name in required_dirs):
            print("API依赖已存在，跳过克隆。")
            return

        temp_path = tempfile.mkdtemp(prefix="API_")
        try:
            Git().clone(Config.API_repositories, temp_path)

            for name in required_dirs:
                source = os.path.join(temp_path, name)
                target = os.path.join(".", name)

                if not os.path.exists(source):
                    raise RuntimeError(f"API仓库缺少目录: {name}")

                if os.path.exists(target):
                    if os.path.isdir(target):
                        shutil.rmtree(target)
                    else:
                        os.remove(target)

                shutil.move(source, target)
        finally:
            shutil.rmtree(temp_path, ignore_errors=True)

    def prepare_flatdata(self):
        print("正在克隆 FlatData 仓库...")

        Git().clone(Config.FlatData_repositories, Config.FlatData_path)

        git = Git(Config.FlatData_path)
        git.checkout(self.server)

        print(f"FlatData 已切换到 {self.server} 分支。")

    def get_key(self):
        from request_api.YostarAPI.QueuingAPI import QueuingAPI as YostarQueuingAPI
        from request_api.NexonAPI.QueuingAPI import QueuingAPI as NexonQueuingAPI

        apk_url, game_version = self.server_info.get_apk_url()

        if self.server == "JP":
            server_config = Config.servers[self.server]
            public_key = Config.YOSTAR_PUBLIC_KEY.encode()
            api_class = YostarQueuingAPI
        else:
            server_config = Config.get_region_config(self.region)
            public_key = Config.NEXON_PUBLIC_KEY.encode()
            api_class = NexonQueuingAPI

        key = secrets.token_bytes(16)
        iv = secrets.token_bytes(16)

        api = api_class(
            server_config["gateway_url"],
            key,
            iv,
            public_key
        )

        if self.server == "JP":
            result = api.Queuing_GetAuthTicket(
                YostarUID=33027791391,
                YostarToken=os.getenv("YostarToken"),
                ClientVersion=game_version
            )
        else:
            result = api.Queuing_GetCryptoKeys()

        print(f"服务器: {self.server}")
        print(f"密钥: {result['EncryptedSqlCipherKey']}")

        return result["EncryptedSqlCipherKey"]

    def download_table(self):
        files = self.download.get_table_files(
            ["ExcelDB.db", "Excel.zip"],
            save_path=self.temp_path,
            workers=2
        )

        if files is None:
            raise RuntimeError("下载地址不存在。")

        if any(value is None for value in files.values()):
            raise RuntimeError(f"下载地址不存在: {files}")

        if all(value is True for value in files.values()):
            return True

        if any(value is False for value in files.values()):
            return False

        raise RuntimeError(f"未知的下载状态: {files}")

    def wait_for_download(self, timeout=5 * 60 * 60, interval=30):
        start_time = time.time()

        while time.time() - start_time < timeout:
            result = self.download_table()

            if result is True:
                print("下载完成。")
                return True

            print("服务器暂未开放，30秒后重试。")
            time.sleep(interval)

        print(f"等待超时，触发 {self.dispatch_type}。")

        Git().dispatch(self.dispatch_type, {"server": self.server, "type": "Table"})

        print("检查结束，程序退出。")
        return False

    def prepare_table(self):
        self.prepare_flatdata()

        return TableExtract(
            server=self.server,
            password=self.key,
            table_file_folder=self.temp_path,
            extract_folder=self.output_path,
            flat_data_module_name=Config.FlatData
        )

    def cleanup(self):
        if self.temp_path:
            shutil.rmtree(self.temp_path, ignore_errors=True)

        if self.output_path:
            shutil.rmtree(self.output_path, ignore_errors=True)

        print("临时文件夹已删除。")

    def run(self):
        self.prepare()

        if not self.wait_for_download():
            return None

        return self.prepare_table()
