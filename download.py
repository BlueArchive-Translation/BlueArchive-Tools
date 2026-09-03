import io
import json
import os
import zipfile
import requests
from dotenv import load_dotenv
from utils.config import Config
from utils.catalog import CNCatalog


class ResourceDownloader:
    def __init__(self, server):
        self.server = server
        self.env_file = Config.env_file.format(server=self.server)
        load_dotenv(self.env_file, override=True)
        self.addressable_catalog_url = os.getenv("AddressableCatalogUrl")

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
        if self.server in ("JP", "JPPC"):
            url = f"{self.addressable_catalog_url}/TableBundles/TableCatalog.bytes"
        elif self.server == "GL":
            url = f"{self.addressable_catalog_url}/Catalog/TableBundles/TableCatalog.bytes"
        elif self.server == "CN":
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

    def get_media_catalog(self, device, save_path=None, to_json=True):
        if device not in ("Android", "iOS", "Windows"):
            raise ValueError(f"不支持的设备类型: {device}")

        if self.server in ("JP", "JPPC"):
            if device in ("Android", "iOS"):
                url = f"{self.addressable_catalog_url}/MediaResources/Catalog/MediaCatalog.bytes"
            elif device == "Windows":
                url = f"{self.addressable_catalog_url}/MediaResources-Windows/Catalog/MediaCatalog.bytes"
        elif self.server == "GL":
            if device in ("Android", "iOS"):
                url = f"{self.addressable_catalog_url}/Catalog/MediaResources/MediaCatalog.bytes"
            elif device == "Windows":
                print("国际服Windows端暂待开发。")
                return None
        elif self.server == "CN":
            media_version = os.getenv("MediaVersion")
            url = f"{self.addressable_catalog_url}/Manifest/MediaResources/{media_version}/MediaManifest"
        else:
            raise ValueError(f"不支持的服务器: {self.server}")

        content = self._download(url)
        if content is None or content is False:
            return content

        if self.server == "CN" and to_json:
            content = CNCatalog().parse_media_manifest(content.decode("utf-8"))

        if save_path:
            self._save(content, save_path)
            return True

        return content

    def get_bundle_packing(self, device, save_path=None):
        if device not in ("Android", "iOS", "Windows"):
            raise ValueError(f"不支持的设备类型: {device}")

        if self.server in ("JP", "JPPC"):
            url = f"{self.addressable_catalog_url}/{device}_PatchPack/BundlePackingInfo.bytes"
        elif self.server == "GL":
            url = os.getenv("ServerInfoDataUrl")
        elif self.server == "CN":
            resource_version = os.getenv("ResourceVersion")
            url = f"{self.addressable_catalog_url}/AssetBundles/Catalog/{resource_version}/{device}/bundleDownloadInfo.json"
        else:
            raise ValueError(f"不支持的服务器: {self.server}")

        content = self._download(url)
        if content is None or content is False:
            return content

        if save_path:
            self._save(content, save_path)
            return True

        return content

    def get_bundle_catalog(self, device, extract=True, save_path=None):
        if device not in ("Android", "iOS", "Windows"):
            raise ValueError(f"不支持的设备类型: {device}")

        if self.server in ("JP", "JPPC"):
            url = f"{self.addressable_catalog_url}/{device}_PatchPack/catalog_{device}.zip"
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
