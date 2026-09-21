import io
import json
import os
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv
from utils.config import Config
from utils.catalog import CNCatalog
from utils.util import FileDownloader


class ResourceDownloader:
    def __init__(self, server, verbose=False):
        self.server = server
        self.config = Config.servers.get(server)
        if not self.config:
            raise ValueError(f"不支持的服务器: {self.server}")
        self.regions = self.config["region"]
        self.platform = self.config["platform"]
        self.verbose = verbose
        self.env_file = Config.env_file.format(server=self.server)
        load_dotenv(self.env_file, override=True)
        self.addressable_catalog_url = os.getenv("AddressableCatalogUrl")

    def _download(self, url, save_path=None):
        downloader = FileDownloader(url, verbose=self.verbose)
        if save_path:
            if downloader.save_file(save_path):
                return True
            response = downloader.get_response()
        else:
            response = downloader.get_response()
        if response is False:
            return False
        if response.status_code == 404:
            print("未知的地址。")
            return None
        if response.status_code == 403:
            print("访问被拒绝。")
            return False
        response.raise_for_status()
        if save_path:
            return True
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
                url = f"{self.addressable_catalog_url}/pool/TableBundles/{crc[:2]}/{crc}"
            else:
                return file, None
            if save_path:
                file_path = os.path.join(save_path, file)
                os.makedirs(os.path.dirname(file_path) or ".", exist_ok=True)
                content = self._download(url, file_path)
                return file, content
            return file, self._download(url)

        results = {}
        with ThreadPoolExecutor(max_workers=workers) as executor:
            for file, content in executor.map(download_file, files):
                results[file] = content
        return results

    def get_media_catalog(self, save_path=None, to_json=True):
        if self.regions == "JP":
            if self.platform in ("Android", "iOS"):
                url = f"{self.addressable_catalog_url}/MediaResources/Catalog/MediaCatalog.bytes"
            elif self.platform == "Windows":
                url = f"{self.addressable_catalog_url}/MediaResources-Windows/Catalog/MediaCatalog.bytes"
        elif self.regions == "GL":
            if self.platform in ("Android"):
                url = f"{self.addressable_catalog_url}/Catalog/MediaResources/MediaCatalog.bytes"
            elif self.platform == "Windows":
                raise ValueError(f"不支持的服务器: {self.server}")
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
                if self.platform in ("Android", "iOS"):
                    url = f"{self.addressable_catalog_url}/MediaResources/{file}"
                elif self.platform == "Windows":
                    url = f"{self.addressable_catalog_url}/MediaResources-Windows/{file}"
            elif self.regions == "CN":
                crc = str(catalog.get(file.lower(), {}).get("Hash", ""))
                url = f"{self.addressable_catalog_url}/pool/MediaResources/{crc[:2]}/{crc}"
            else:
                return file, None
            if save_path:
                file_path = os.path.join(save_path, file)
                os.makedirs(os.path.dirname(file_path) or ".", exist_ok=True)
                content = self._download(url, file_path)
                return file, content
            return file, self._download(url)

        results = {}
        with ThreadPoolExecutor(max_workers=workers) as executor:
            for file, content in executor.map(download_file, files):
                results[file] = content
        return results

    def get_bundle_packing(self, save_path=None):
        if self.regions == "JP":
            url = f"{self.addressable_catalog_url}/{self.platform}_PatchPack/BundlePackingInfo.bytes"
        elif self.regions == "GL":
            url = os.getenv("ServerInfoDataUrl")
        elif self.regions == "CN":
            resource_version = os.getenv("ResourceVersion")
            url = f"{self.addressable_catalog_url}/AssetBundles/Catalog/{resource_version}/{self.platform}/bundleDownloadInfo.json"
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
            url = f"{self.addressable_catalog_url}/{self.platform}_PatchPack/catalog_{self.platform}.zip"
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
                url = f"{self.addressable_catalog_url}/{self.platform}_PatchPack/{file}"
            elif self.regions == "CN":
                url = f"{self.addressable_catalog_url}/AssetBundles/{self.platform}/{file}"
            else:
                return file, None
            if save_path:
                file_path = os.path.join(save_path, file)
                os.makedirs(os.path.dirname(file_path) or ".", exist_ok=True)
                content = self._download(url, file_path)
                return file, content
            return file, self._download(url)

        results = {}
        with ThreadPoolExecutor(max_workers=workers) as executor:
            for file, content in executor.map(download_file, files):
                results[file] = content
        return results
