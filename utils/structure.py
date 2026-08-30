from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, IntEnum
from typing import Any, Iterator, Literal, NewType, overload
from urllib.parse import urljoin


# ============================================================
# Primitive Type Aliases
# ============================================================

# MemoryPack / C# int32
Int32 = NewType("Int32", int)


# ============================================================
# Enums
# ============================================================


class MediaType(IntEnum):
    None_ = 0
    Audio = 1
    Video = 2
    Texture = 3


class StorageType(IntEnum):
    None_ = 0
    InBuild = 1
    Preload = 2
    GameData = 3


class SQLiteDataType(Enum):
    INTEGER = int
    REAL = float
    NUMERIC = float
    TEXT = str
    BLOB = bytes
    BOOLEAN = bool
    NULL = None


# ============================================================
# Table
# ============================================================


@dataclass
class TableBundle:
    Name: str = ""
    Size: int = 0
    Crc: int = 0
    isInbuild: bool = False
    isChanged: bool = False
    IsPrologue: bool = False
    IsSplitDownload: bool = False
    Includes: list[str] = field(default_factory=list)


@dataclass
class TablePatchPack:
    Name: str = ""
    Size: int = 0
    Crc: int = 0
    IsPrologue: bool = False
    BundleFiles: list[TableBundle] = field(default_factory=list)


@dataclass
class TableCatalog:
    Table: dict[str, TableBundle] = field(default_factory=dict)
    TablePack: dict[str, TablePatchPack] = field(default_factory=dict)


# ============================================================
# Table GL
# ============================================================


@dataclass
class TableBundleGL:
    Name: str = ""
    Crc: int = 0
    IsPrologue: bool = False
    Includes: list[str] = field(default_factory=list)


@dataclass
class TableCatalogGL:
    Table: dict[str, TableBundleGL] = field(default_factory=dict)
    Catalog: dict[str, TableBundleGL] = field(default_factory=dict)


# ============================================================
# Media
# ============================================================


@dataclass
class Media:
    Path: str = ""
    FileName: str = ""
    Bytes: int = 0
    Crc: int = 0
    IsPrologue: bool = False
    IsSplitDownload: bool = False
    MediaType: MediaType = MediaType.None_


@dataclass
class MediaCatalog:
    Table: dict[str, Media] = field(default_factory=dict)


# ============================================================
# Media GL
# ============================================================


@dataclass
class MediaGL:
    Path: str = ""
    StorageType: StorageType = StorageType.None_
    MediaType: MediaType = MediaType.None_


@dataclass
class MediaCatalogGL:
    Table: dict[str, MediaGL] = field(default_factory=dict)
    Catalog: dict[str, MediaGL] = field(default_factory=dict)


# ============================================================
# Bundle
# ============================================================


@dataclass
class BundleFile:
    Name: str = ""
    Size: int = 0
    IsPrologue: bool = False
    Crc: int = 0
    IsSplitDownload: bool = False
    FileHash: int = 0
    Signature: str = ""


@dataclass
class BundlePatchPack:
    PackName: str = ""
    PackSize: int = 0
    Crc: int = 0
    IsPrologue: bool = False
    IsSplitDownload: bool = False
    BundleFiles: list[BundleFile] = field(default_factory=list)


@dataclass
class BundlePatchPackInfo:
    Milestone: str = ""

    # C# int / Int32
    PatchVersion: Int32 = 0

    FullPatchPacks: list[BundlePatchPack] = field(
        default_factory=list
    )

    UpdatePacks: list[BundlePatchPack] = field(
        default_factory=list
    )


# ============================================================
# Database
# ============================================================


@dataclass
class DBColumn:
    name: str
    data_type: str


@dataclass
class DBTable:
    name: str
    columns: list[DBColumn]
    data: list[list]


# ============================================================
# Compiler
# ============================================================


@dataclass
class Property:
    data_type: str
    name: str
    is_list: bool


@dataclass
class StructTable:
    name: str
    properties: list[Property]


@dataclass
class EnumMember:
    name: str
    value: str


@dataclass
class EnumType:
    name: str
    underlying_type: str
    members: list[EnumMember]


# ============================================================
# Export
# ============================================================


__all__ = [
    "Int32",

    "MediaType",
    "StorageType",
    "SQLiteDataType",

    "TableBundle",
    "TablePatchPack",
    "TableCatalog",

    "TableBundleGL",
    "TableCatalogGL",

    "Media",
    "MediaCatalog",

    "MediaGL",
    "MediaCatalogGL",

    "BundleFile",
    "BundlePatchPack",
    "BundlePatchPackInfo",

    "DBColumn",
    "DBTable",

    "Property",
    "StructTable",
    "EnumMember",
    "EnumType",
]
