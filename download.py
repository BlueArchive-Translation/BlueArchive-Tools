import io
import json
import os
import zipfile
import requests
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv
from utils.config import Config
from utils.catalog import CNCatalog


class ResourceDownloader:
    def __init__(self, server):
        self.server = server

        if self.server in ("JP", "JPPC", "JPiOS"):
            self.regions = "JP"
        elif self.server in ("GL", "GLPC", "GLiOS"):
            self.regions = "GL"
        elif self.server == "CN":
            self.regions = "CN"
        else:
            raise ValueError(f"不支持的服务器: {self.server}")

        self.env_file = Config.env_file.format(server=self.server)
        load_dotenv(self.env_file, override=True)

        self.addressable_catalog_url = os.getenv("AddressableCatalogUrl")

        if self.server in ("JP", "GL", "CN"):
            self.device = "Android"
        elif self.server in ("JPPC", "GLPC"):
            self.device = "Windows"
        elif self.server in ("JPiOS", "GLiOS"):
            self.device = "iOS"
        else:
            raise ValueError(f"不支持的服务器: {self.server}")

    def _download(self, url):
        response = requests.get(url)
        if response.status_code == 404:
            print("未知的地址。")
            return None
        if response.status_code == 403:
            print("访问被拒绝。")
            return False
        response.raise_for_status()
        return response.content

    def _save(self, content, save_path):
        os.makedirs(os.path.dirname(save_path) or ".", exist_ok=True)
        if isinstance(content, str):
            with open(save_path, "w", encoding="utf-8") as f:
                f.write(content)
        else:
            with open(save_path, "wb") as f:
                f.write(content)

    def get_table_catalog(self, save_path=None):
        if self.regions == "JP":
            url = f"{self.addressable_catalog_url}/TableBundles/TableCatalog.bytes"
        elif self.regions == "GL":
            url = f"{self.addressable_catalog_url}/Catalog/TableBundles/TableCatalog.bytes"
        elif self.regions == "CN":
            table_version = os.getenv("TableVersion")
            url = f"{self.addressable_catalog_url}/Manifest/TableBundles/{table_version}/TableManifest"
        else:
            raise ValueError(f"不支持的服务器: {self.server}")

        content = self._download(url)
        if content is None or content is False:
            return content

        if save_path:
            self._save(content, save_path)
            return True
        return content

    def get_table_files(self, files, save_path=None, workers=1):
        if not isinstance(files, list):
            raise ValueError("files 必须为列表。")

        catalog = None
        if self.regions == "CN":
            catalog = json.loads(self.get_table_catalog().decode("utf-8"))

        def download_file(file):
            if self.regions == "JP":
                url = f"{self.addressable_catalog_url}/TableBundles/{file}"
            elif self.regions == "GL":
                url = f"{self.addressable_catalog_url}/Preload/TableBundles/{file}"
            elif self.regions == "CN":
                crc = str(catalog.get("Table", {}).get(file, {}).get("Crc", ""))
                url = f"{self.addressable_catalog_url}/pool/MediaResources/{crc[:2]}/{crc}"
            else:
                return file, None

            content = self._download(url)
            if content is None or content is False:
                return file, content

            if save_path:
                file_path = os.path.join(save_path, file)
                self._save(content, file_path)
                return file, True

            return file, content

        results = {}
        with ThreadPoolExecutor(max_workers=workers) as executor:
            for file, content in executor.map(download_file, files):
                results[file] = content

        return results

    def get_media_catalog(self, save_path=None, to_json=True):
        if self.regions == "JP":
            if self.device in ("Android", "iOS"):
                url = f"{self.addressable_catalog_url}/MediaResources/Catalog/MediaCatalog.bytes"
            elif self.device == "Windows":
                url = f"{self.addressable_catalog_url}/MediaResources-Windows/Catalog/MediaCatalog.bytes"

        elif self.regions == "GL":
            if self.device in ("Android"):
                url = f"{self.addressable_catalog_url}/Catalog/MediaResources/MediaCatalog.bytes"
            elif self.device == "Windows":
                raise ValueError(f"不支持的服务器: {self.server}")
                return None

        elif self.regions == "CN":
            media_version = os.getenv("MediaVersion")
            url = f"{self.addressable_catalog_url}/Manifest/MediaResources/{media_version}/MediaManifest"

        else:
            raise ValueError(f"不支持的服务器: {self.server}")

        content = self._download(url)
        if content is None or content is False:
            return content

        if self.regions == "CN" and to_json:
            content = CNCatalog().parse_media_manifest(content.decode("utf-8"))

        if save_path:
            self._save(content, save_path)
            return True

        return content

    def get_media_files(self, files, save_path=None, workers=1):
        if not isinstance(files, list):
            raise ValueError("files 必须为列表。")

        catalog = None
        if self.regions == "CN":
            catalog = self.get_media_catalog(to_json=True)

        def download_file(file):
            if self.regions == "JP":
                if self.device in ("Android", "iOS"):
                    url = f"{self.addressable_catalog_url}/MediaResources/{file}"
                elif self.device == "Windows":
                    url = f"{self.addressable_catalog_url}/MediaResources-Windows/{file}"
            elif self.regions == "CN":
                crc = str(catalog.get(file.lower(), {}).get("Hash", ""))
                url = f"{self.addressable_catalog_url}/pool/MediaResources/{crc[:2]}/{crc}"
            else:
                return file, None

            content = self._download(url)
            if content is None or content is False:
                return file, content

            if save_path:
                file_path = os.path.join(save_path, file)
                self._save(content, file_path)
                return file, True

            return file, content

        results = {}
        with ThreadPoolExecutor(max_workers=workers) as executor:
            for file, content in executor.map(download_file, files):
                results[file] = content

        return results

    def get_bundle_packing(self, save_path=None):
        if self.regions == "JP":
            url = f"{self.addressable_catalog_url}/{self.device}_PatchPack/BundlePackingInfo.bytes"
        elif self.regions == "GL":
            url = os.getenv("ServerInfoDataUrl")
        elif self.regions == "CN":
            resource_version = os.getenv("ResourceVersion")
            url = f"{self.addressable_catalog_url}/AssetBundles/Catalog/{resource_version}/{self.device}/bundleDownloadInfo.json"
        else:
            raise ValueError(f"不支持的服务器: {self.server}")

        content = self._download(url)
        if content is None or content is False:
            return content

        if save_path:
            self._save(content, save_path)
            return True

        return content

    def get_bundle_catalog(self, extract=True, save_path=None):
        if self.regions == "JP":
            url = f"{self.addressable_catalog_url}/{self.device}_PatchPack/catalog_{self.device}.zip"
        else:
            raise ValueError(f"暂不支持的服务器: {self.server}")

        content = self._download(url)
        if content is None or content is False:
            return content

        if extract:
            with zipfile.ZipFile(io.BytesIO(content)) as zf:
                files = zf.namelist()
                if len(files) != 1:
                    raise ValueError("BundleCatalog ZIP 文件内容异常。")
                content = zf.read(files[0]).decode("utf-8")

        if save_path:
            self._save(content, save_path)
            return True

        return content

    def get_bundle_files(self, files, save_path=None, workers=1):
        if not isinstance(files, list):
            raise ValueError("files 必须为列表。")

        def download_file(file):
            if self.regions == "JP":
                url = f"{self.addressable_catalog_url}/{self.device}_PatchPack/{file}"
            elif self.regions == "CN":
                url = f"{self.addressable_catalog_url}/AssetBundles/{self.device}/{file}"
            else:
                return file, None

            content = self._download(url)
            if content is None or content is False:
                return file, content

            if save_path:
                file_path = os.path.join(save_path, file)
                self._save(content, file_path)
                return file, True

            return file, content

        results = {}
        with ThreadPoolExecutor(max_workers=workers) as executor:
            for file, content in executor.map(download_file, files):
                results[file] = content

        return results
