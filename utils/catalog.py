import io
import struct
import json
import os

from utils.util import CommandUtils, ZipUtils, ToolManager, FileDownloader
from utils.memorypack import MemoryPack
from utils.structure import (
    TableCatalog,
    MediaCatalog,
    BundlePatchPackInfo,
    TableCatalogGL,
    MediaCatalogGL
)


class CNCatalog:
    def __init__(self):
        self.media_type = {
            0: "none",
            1: "ogg",
            2: "mp4",
            3: "jpg",
            4: "png",
            5: "acb",
            6: "awb"
        }

    def parse_media_manifest(self, raw_data):
        lines = raw_data.strip().split('\n')
        result = {}

        for line in lines:
            if not line.strip():
                continue

            parts = [p.strip() for p in line.rstrip(',').split(',')]

            if len(parts) >= 4:
                # 为确保 Key 值唯一，故此进行文件后缀拼接
                raw_key = parts[0]
                m_type_value = int(parts[2])
                media_type = self.media_type.get(
                    m_type_value,
                    str(m_type_value)
                )

                unique_key = f"{raw_key}.{media_type}"

                result[unique_key] = {
                    "Hash": parts[1],
                    "MediaType": media_type,
                    "Size": int(parts[3])
                }

        return json.dumps(
            result,
            indent=4,
            ensure_ascii=False
        )

    def restore_media_manifest(self, json_data):
        if isinstance(json_data, str):
            data = json.loads(json_data)
        else:
            data = json_data

        reverse_media_type = {
            value: key
            for key, value in self.media_type.items()
        }

        lines = []

        for unique_key, info in data.items():
            media_type = info["MediaType"]

            suffix = f".{media_type}"

            if unique_key.endswith(suffix):
                raw_key = unique_key[:-len(suffix)]
            else:
                raw_key = unique_key

            m_type_value = reverse_media_type.get(
                media_type,
                media_type
            )

            lines.append(
                f"{raw_key},{info['Hash']},{m_type_value},{info['Size']}"
            )

        return "\n".join(lines)


class JPCatalog:
    def __init__(self):
        pass

    # ========================================================
    # TableCatalog
    # ========================================================

    def unpack_table_catalog(self, raw_data):
        """
        TableCatalog MemoryPack bytes -> JSON
        """

        catalog = MemoryPack.unpack(
            raw_data,
            TableCatalog,
        )

        result = MemoryPack.to_dict(
            catalog
        )

        return json.dumps(
            result,
            ensure_ascii=False,
            indent=4,
        )

    def pack_table_catalog(self, json_data):
        """
        TableCatalog JSON -> MemoryPack bytes
        """

        if isinstance(json_data, str):
            data = json.loads(json_data)
        else:
            data = json_data

        catalog = MemoryPack.from_dict(
            data,
            TableCatalog,
        )

        return MemoryPack.pack(
            catalog
        )

    # ========================================================
    # MediaCatalog
    # ========================================================

    def unpack_media_catalog(self, raw_data):
        """
        MediaCatalog MemoryPack bytes -> JSON
        """

        catalog = MemoryPack.unpack(
            raw_data,
            MediaCatalog,
        )

        result = MemoryPack.to_dict(
            catalog
        )

        return json.dumps(
            result,
            ensure_ascii=False,
            indent=4,
        )

    def pack_media_catalog(self, json_data):
        """
        MediaCatalog JSON -> MemoryPack bytes
        """

        if isinstance(json_data, str):
            data = json.loads(json_data)
        else:
            data = json_data

        catalog = MemoryPack.from_dict(
            data,
            MediaCatalog,
        )

        return MemoryPack.pack(
            catalog
        )

    # ========================================================
    # BundlePackingInfo
    # ========================================================

    def unpack_bundle_packing_info(self, raw_data):
        """
        BundlePackingInfo MemoryPack bytes -> JSON
        """

        info = MemoryPack.unpack(
            raw_data,
            BundlePatchPackInfo,
        )

        result = MemoryPack.to_dict(
            info
        )

        return json.dumps(
            result,
            ensure_ascii=False,
            indent=4,
        )

    def pack_bundle_packing_info(self, json_data):
        """
        BundlePackingInfo JSON -> MemoryPack bytes
        """

        if isinstance(json_data, str):
            data = json.loads(json_data)
        else:
            data = json_data

        info = MemoryPack.from_dict(
            data,
            BundlePatchPackInfo,
        )

        return MemoryPack.pack(
            info
        )


class GLCatalog:
    def __init__(self):
        pass

    # ========================================================
    # TableCatalogGL
    # ========================================================

    def unpack_table_catalog(self, raw_data):
        """
        TableCatalogGL MemoryPack bytes -> JSON
        """

        catalog = MemoryPack.unpack(
            raw_data,
            TableCatalogGL,
        )

        result = MemoryPack.to_dict(
            catalog
        )

        return json.dumps(
            result,
            ensure_ascii=False,
            indent=4,
        )

    def pack_table_catalog(self, json_data):
        """
        TableCatalogGL JSON -> MemoryPack bytes
        """

        if isinstance(json_data, str):
            data = json.loads(json_data)
        else:
            data = json_data

        catalog = MemoryPack.from_dict(
            data,
            TableCatalogGL,
        )

        return MemoryPack.pack(
            catalog
        )

    # ========================================================
    # MediaCatalogGL
    # ========================================================

    def unpack_media_catalog(self, raw_data):
        """
        MediaCatalogGL MemoryPack bytes -> JSON
        """

        catalog = MemoryPack.unpack(
            raw_data,
            MediaCatalogGL,
        )

        result = MemoryPack.to_dict(
            catalog
        )

        return json.dumps(
            result,
            ensure_ascii=False,
            indent=4,
        )

    def pack_media_catalog(self, json_data):
        """
        MediaCatalogGL JSON -> MemoryPack bytes
        """

        if isinstance(json_data, str):
            data = json.loads(json_data)
        else:
            data = json_data

        catalog = MemoryPack.from_dict(
            data,
            MediaCatalogGL,
        )

        return MemoryPack.pack(
            catalog
        )
