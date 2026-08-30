from __future__ import annotations

import json
import struct
import types

from dataclasses import fields, is_dataclass
from enum import IntEnum
from typing import (
    Any,
    Optional,
    Union,
    get_args,
    get_origin,
    get_type_hints,
)


# ============================================================
# Exception
# ============================================================


class MemoryPackError(Exception):
    """MemoryPack error."""


class MemoryPackEOFError(MemoryPackError):
    """Unexpected end of MemoryPack data."""


# ============================================================
# Special Integer Types
# ============================================================

# 使用独立类型表示 C# int32。
#
# Python:
#     Int32
#
# C#:
#     int
#
# 而普通 Python int 在本项目中继续按照 C# long 处理。
#
# 不直接 import memorypack_structures，避免循环依赖。


def _is_int32_type(tp) -> bool:
    """
    判断是否为 memorypack_structures.Int32。

    Int32 是 typing.NewType 创建出来的函数，
    因此通过 __name__ / __supertype__ 判断。
    """

    return (
        getattr(tp, "__name__", None) == "Int32"
        and getattr(tp, "__supertype__", None) is int
    )


# ============================================================
# Writer
# ============================================================


class Writer:

    def __init__(self):
        self.data = bytearray()

    # --------------------------------------------------------
    # Raw
    # --------------------------------------------------------

    def write(self, value: bytes):
        self.data.extend(value)

    # --------------------------------------------------------
    # Primitive
    # --------------------------------------------------------

    def byte(self, value: int):
        self.data.append(value & 0xFF)

    def bool(self, value: bool):
        self.byte(1 if value else 0)

    def int32(self, value: int):
        self.write(
            struct.pack(
                "<i",
                int(value),
            )
        )

    def uint32(self, value: int):
        self.write(
            struct.pack(
                "<I",
                int(value),
            )
        )

    def int64(self, value: int):
        self.write(
            struct.pack(
                "<q",
                int(value),
            )
        )

    def uint64(self, value: int):
        self.write(
            struct.pack(
                "<Q",
                int(value),
            )
        )

    def float32(self, value: float):
        self.write(
            struct.pack(
                "<f",
                float(value),
            )
        )

    def float64(self, value: float):
        self.write(
            struct.pack(
                "<d",
                float(value),
            )
        )

    # --------------------------------------------------------

    def bytes(self) -> bytes:
        return bytes(self.data)


# ============================================================
# Reader
# ============================================================


class Reader:

    def __init__(self, data: bytes):
        self.data = memoryview(data)
        self.offset = 0

    # --------------------------------------------------------

    def remaining(self) -> int:
        return len(self.data) - self.offset

    # --------------------------------------------------------

    def read(self, size: int) -> bytes:

        if size < 0:
            raise MemoryPackError(
                f"Negative read size: {size}"
            )

        end = self.offset + size

        if end > len(self.data):
            raise MemoryPackEOFError(
                "Unexpected EOF: "
                f"offset={self.offset}, "
                f"need={size}, "
                f"remaining={self.remaining()}"
            )

        result = self.data[
            self.offset:end
        ].tobytes()

        self.offset = end

        return result

    # --------------------------------------------------------

    def byte(self) -> int:
        return self.read(1)[0]

    def bool(self) -> bool:
        return self.byte() != 0

    def int32(self) -> int:
        return struct.unpack(
            "<i",
            self.read(4),
        )[0]

    def uint32(self) -> int:
        return struct.unpack(
            "<I",
            self.read(4),
        )[0]

    def int64(self) -> int:
        return struct.unpack(
            "<q",
            self.read(8),
        )[0]

    def uint64(self) -> int:
        return struct.unpack(
            "<Q",
            self.read(8),
        )[0]

    def float32(self) -> float:
        return struct.unpack(
            "<f",
            self.read(4),
        )[0]

    def float64(self) -> float:
        return struct.unpack(
            "<d",
            self.read(8),
        )[0]


# ============================================================
# MemoryPack
# ============================================================


class MemoryPack:

    # ========================================================
    # Public API
    # ========================================================

    @classmethod
    def serialize(
        cls,
        value: Any,
    ) -> bytes:

        writer = Writer()

        cls._write_value(
            writer,
            type(value),
            value,
        )

        return writer.bytes()

    pack = serialize

    # --------------------------------------------------------

    @classmethod
    def deserialize(
        cls,
        data: bytes,
        target_type,
    ):

        target_type = cls._resolve_type(
            target_type
        )

        reader = Reader(data)

        result = cls._read_value(
            reader,
            target_type,
        )

        if reader.remaining() != 0:

            raise MemoryPackError(
                "Trailing data: "
                f"{reader.remaining()} bytes "
                f"at offset {reader.offset}"
            )

        return result

    unpack = deserialize

    # --------------------------------------------------------

    @classmethod
    def load(
        cls,
        filename: str,
        target_type,
    ):

        with open(
            filename,
            "rb",
        ) as f:

            data = f.read()

        return cls.deserialize(
            data,
            target_type,
        )

    # --------------------------------------------------------

    @classmethod
    def dump(
        cls,
        filename: str,
        value: Any,
    ):

        data = cls.serialize(value)

        with open(
            filename,
            "wb",
        ) as f:

            f.write(data)

    # ========================================================
    # Type Resolution
    # ========================================================

    @staticmethod
    def _resolve_type(tp):

        if isinstance(tp, str):

            raise MemoryPackError(
                f"Unresolved type annotation: {tp!r}"
            )

        return tp

    # ========================================================
    # Optional
    # ========================================================

    @staticmethod
    def _is_optional(tp):

        origin = get_origin(tp)

        if origin in (
            Union,
            types.UnionType,
        ):

            args = get_args(tp)

            return (
                type(None) in args
                and len(args) == 2
            )

        return False

    # --------------------------------------------------------

    @staticmethod
    def _optional_type(tp):

        for arg in get_args(tp):

            if arg is not type(None):
                return arg

        return Any

    # ========================================================
    # List
    # ========================================================

    @staticmethod
    def _is_list(tp):

        origin = get_origin(tp)

        return origin in (
            list,
            tuple,
        )

    # --------------------------------------------------------

    @staticmethod
    def _list_type(tp):

        args = get_args(tp)

        if args:
            return args[0]

        return Any

    # ========================================================
    # Dictionary
    # ========================================================

    @staticmethod
    def _is_dict(tp):

        origin = get_origin(tp)

        return (
            origin is dict
            or tp is dict
        )

    # --------------------------------------------------------

    @staticmethod
    def _dict_types(tp):

        args = get_args(tp)

        if len(args) == 2:

            return (
                args[0],
                args[1],
            )

        return Any, Any

    # ========================================================
    # String
    # ========================================================

    @classmethod
    def _write_string(
        cls,
        w: Writer,
        value: Optional[str],
    ):

        if value is None:

            w.int32(-1)

            return

        if value == "":

            w.int32(0)

            return

        encoded = value.encode(
            "utf-8"
        )

        utf8_length = len(
            encoded
        )

        utf16_length = (
            len(
                value.encode(
                    "utf-16-le"
                )
            )
            // 2
        )

        w.int32(
            ~utf8_length
        )

        w.int32(
            utf16_length
        )

        w.write(
            encoded
        )

    # --------------------------------------------------------

    @classmethod
    def _read_string(
        cls,
        r: Reader,
    ):

        first = r.int32()

        if first == -1:
            return None

        if first == 0:
            return ""

        if first < -1:

            byte_length = ~first

            if byte_length < 0:

                raise MemoryPackError(
                    f"Invalid string byte length: "
                    f"{byte_length}"
                )

            r.int32()

            raw = r.read(
                byte_length
            )

            try:

                return raw.decode(
                    "utf-8"
                )

            except UnicodeDecodeError as e:

                raise MemoryPackError(
                    f"Invalid UTF-8 string: {e}"
                ) from e

        raise MemoryPackError(
            f"Invalid MemoryPack string header: "
            f"{first}"
        )

    # ========================================================
    # Collection
    # ========================================================

    @staticmethod
    def _write_collection_length(
        w: Writer,
        length: Optional[int],
    ):

        if length is None:

            w.int32(-1)

        else:

            w.int32(
                int(length)
            )

    # --------------------------------------------------------

    @staticmethod
    def _read_collection_length(
        r: Reader,
    ):

        length = r.int32()

        if length == -1:
            return None

        if length < -1:

            raise MemoryPackError(
                f"Invalid collection length: "
                f"{length}"
            )

        return length

    # ========================================================
    # Object
    # ========================================================

    @classmethod
    def _write_object(
        cls,
        w: Writer,
        tp,
        value,
    ):

        if value is None:

            w.byte(0xFF)

            return

        if not is_dataclass(value):

            raise MemoryPackError(
                f"{tp!r} is not a dataclass"
            )

        obj_fields = fields(
            value
        )

        member_count = len(
            obj_fields
        )

        if member_count > 249:

            raise MemoryPackError(
                f"Too many object fields: "
                f"{member_count}"
            )

        w.byte(
            member_count
        )

        try:

            hints = get_type_hints(
                tp
            )

        except Exception:

            hints = {}

        for f in obj_fields:

            field_type = hints.get(
                f.name,
                f.type,
            )

            field_type = cls._resolve_type(
                field_type
            )

            field_value = getattr(
                value,
                f.name,
            )

            cls._write_value(
                w,
                field_type,
                field_value,
            )

    # --------------------------------------------------------

    @classmethod
    def _read_object(
        cls,
        r: Reader,
        tp,
    ):

        start_offset = r.offset

        header = r.byte()

        if header == 0xFF:
            return None

        if header == 0xFA:

            raise MemoryPackError(
                "Circular reference is not supported."
            )

        if header > 249:

            raise MemoryPackError(
                f"Invalid object header: "
                f"{header}"
            )

        obj_fields = fields(
            tp
        )

        expected = len(
            obj_fields
        )

        if header != expected:

            raise MemoryPackError(
                f"{tp.__name__}: "
                f"expected {expected} fields, "
                f"got {header}, "
                f"object_offset={start_offset}"
            )

        try:

            hints = get_type_hints(
                tp
            )

        except Exception as e:

            raise MemoryPackError(
                f"Cannot resolve type hints "
                f"for {tp}: {e}"
            ) from e

        values = {}

        for f in obj_fields:

            field_type = hints.get(
                f.name,
                f.type,
            )

            field_type = cls._resolve_type(
                field_type
            )

            values[f.name] = cls._read_value(
                r,
                field_type,
            )

        return tp(
            **values
        )

    # ========================================================
    # Write Value
    # ========================================================

    @classmethod
    def _write_value(
        cls,
        w: Writer,
        tp,
        value,
    ):

        tp = cls._resolve_type(
            tp
        )

        # ----------------------------------------------------
        # Optional
        # ----------------------------------------------------

        if cls._is_optional(tp):

            inner = cls._optional_type(
                tp
            )

            if value is None:

                if inner is str:

                    cls._write_string(
                        w,
                        None,
                    )

                    return

                if cls._is_list(inner):

                    cls._write_collection_length(
                        w,
                        None,
                    )

                    return

                if cls._is_dict(inner):

                    cls._write_collection_length(
                        w,
                        None,
                    )

                    return

                if (
                    isinstance(inner, type)
                    and is_dataclass(inner)
                ):

                    w.byte(0xFF)

                    return

                raise MemoryPackError(
                    f"Cannot serialize None as "
                    f"{tp!r}"
                )

            cls._write_value(
                w,
                inner,
                value,
            )

            return

        # ----------------------------------------------------
        # None
        # ----------------------------------------------------

        if value is None:

            if tp is str:

                cls._write_string(
                    w,
                    None,
                )

                return

            if cls._is_list(tp):

                cls._write_collection_length(
                    w,
                    None,
                )

                return

            if cls._is_dict(tp):

                cls._write_collection_length(
                    w,
                    None,
                )

                return

            if (
                isinstance(tp, type)
                and is_dataclass(tp)
            ):

                w.byte(0xFF)

                return

            raise MemoryPackError(
                f"Cannot serialize None as "
                f"{tp!r}"
            )

        # ----------------------------------------------------
        # Int32
        #
        # C# int
        # 4 bytes
        # ----------------------------------------------------

        if _is_int32_type(tp):

            w.int32(
                value
            )

            return

        # ----------------------------------------------------
        # Enum
        # ----------------------------------------------------

        if (
            isinstance(tp, type)
            and issubclass(tp, IntEnum)
        ):

            w.int32(
                int(value)
            )

            return

        # ----------------------------------------------------
        # Bool
        # ----------------------------------------------------

        if tp is bool:

            w.bool(
                value
            )

            return

        # ----------------------------------------------------
        # Int64
        #
        # 普通 Python int
        # 当前项目对应 C# long
        # ----------------------------------------------------

        if tp is int:

            w.int64(
                value
            )

            return

        # ----------------------------------------------------
        # Float
        # ----------------------------------------------------

        if tp is float:

            w.float64(
                value
            )

            return

        # ----------------------------------------------------
        # String
        # ----------------------------------------------------

        if tp is str:

            cls._write_string(
                w,
                value,
            )

            return

        # ----------------------------------------------------
        # List
        # ----------------------------------------------------

        if cls._is_list(tp):

            item_type = cls._list_type(
                tp
            )

            cls._write_collection_length(
                w,
                len(value),
            )

            for item in value:

                cls._write_value(
                    w,
                    item_type,
                    item,
                )

            return

        # ----------------------------------------------------
        # Dictionary
        # ----------------------------------------------------

        if cls._is_dict(tp):

            key_type, value_type = (
                cls._dict_types(tp)
            )

            cls._write_collection_length(
                w,
                len(value),
            )

            for key, item in value.items():

                cls._write_value(
                    w,
                    key_type,
                    key,
                )

                cls._write_value(
                    w,
                    value_type,
                    item,
                )

            return

        # ----------------------------------------------------
        # Dataclass
        # ----------------------------------------------------

        if (
            isinstance(tp, type)
            and is_dataclass(tp)
        ):

            cls._write_object(
                w,
                tp,
                value,
            )

            return

        raise MemoryPackError(
            f"Unsupported type: {tp!r}"
        )

    # ========================================================
    # Read Value
    # ========================================================

    @classmethod
    def _read_value(
        cls,
        r: Reader,
        tp,
    ):

        tp = cls._resolve_type(
            tp
        )

        # ----------------------------------------------------
        # Optional
        # ----------------------------------------------------

        if cls._is_optional(tp):

            inner = cls._optional_type(
                tp
            )

            return cls._read_value(
                r,
                inner,
            )

        # ----------------------------------------------------
        # Int32
        #
        # C# int
        # 4 bytes
        # ----------------------------------------------------

        if _is_int32_type(tp):

            return r.int32()

        # ----------------------------------------------------
        # Enum
        # ----------------------------------------------------

        if (
            isinstance(tp, type)
            and issubclass(tp, IntEnum)
        ):

            value = r.int32()

            try:

                return tp(value)

            except ValueError:

                return value

        # ----------------------------------------------------
        # Bool
        # ----------------------------------------------------

        if tp is bool:

            return r.bool()

        # ----------------------------------------------------
        # Int64
        #
        # C# long
        # 8 bytes
        # ----------------------------------------------------

        if tp is int:

            return r.int64()

        # ----------------------------------------------------
        # Float
        # ----------------------------------------------------

        if tp is float:

            return r.float64()

        # ----------------------------------------------------
        # String
        # ----------------------------------------------------

        if tp is str:

            return cls._read_string(
                r
            )

        # ----------------------------------------------------
        # List
        # ----------------------------------------------------

        if cls._is_list(tp):

            item_type = cls._list_type(
                tp
            )

            length = cls._read_collection_length(
                r
            )

            if length is None:
                return None

            result = []

            for _ in range(length):

                result.append(
                    cls._read_value(
                        r,
                        item_type,
                    )
                )

            return result

        # ----------------------------------------------------
        # Dictionary
        # ----------------------------------------------------

        if cls._is_dict(tp):

            key_type, value_type = (
                cls._dict_types(tp)
            )

            length = cls._read_collection_length(
                r
            )

            if length is None:
                return None

            result = {}

            for _ in range(length):

                key = cls._read_value(
                    r,
                    key_type,
                )

                value = cls._read_value(
                    r,
                    value_type,
                )

                result[key] = value

            return result

        # ----------------------------------------------------
        # Dataclass
        # ----------------------------------------------------

        if (
            isinstance(tp, type)
            and is_dataclass(tp)
        ):

            return cls._read_object(
                r,
                tp,
            )

        raise MemoryPackError(
            f"Unsupported type: {tp!r}"
        )

    # ========================================================
    # JSON
    # ========================================================

    @classmethod
    def to_dict(
        cls,
        value,
    ):

        if value is None:
            return None

        if isinstance(
            value,
            IntEnum,
        ):

            return int(value)

        if is_dataclass(value):

            return {
                f.name: cls.to_dict(
                    getattr(
                        value,
                        f.name,
                    )
                )
                for f in fields(value)
            }

        if isinstance(
            value,
            dict,
        ):

            return {
                str(key): cls.to_dict(
                    item
                )
                for key, item in value.items()
            }

        if isinstance(
            value,
            (list, tuple),
        ):

            return [
                cls.to_dict(item)
                for item in value
            ]

        return value

    # --------------------------------------------------------

    @classmethod
    def to_json(
        cls,
        value,
        *,
        indent=4,
    ):

        return json.dumps(
            cls.to_dict(value),
            ensure_ascii=False,
            indent=indent,
        )

    # ========================================================
    # JSON -> Object
    # ========================================================

    @classmethod
    def from_dict(
        cls,
        data,
        tp,
    ):

        tp = cls._resolve_type(
            tp
        )

        if data is None:
            return None

        # ----------------------------------------------------
        # Int32
        # ----------------------------------------------------

        if _is_int32_type(tp):

            return int(data)

        # ----------------------------------------------------
        # Optional
        # ----------------------------------------------------

        if cls._is_optional(tp):

            return cls.from_dict(
                data,
                cls._optional_type(tp),
            )

        # ----------------------------------------------------
        # Enum
        # ----------------------------------------------------

        if (
            isinstance(tp, type)
            and issubclass(tp, IntEnum)
        ):

            return tp(
                int(data)
            )

        # ----------------------------------------------------
        # String
        # ----------------------------------------------------

        if tp is str:

            return str(data)

        # ----------------------------------------------------
        # Bool
        # ----------------------------------------------------

        if tp is bool:

            return bool(data)

        # ----------------------------------------------------
        # Int
        # ----------------------------------------------------

        if tp is int:

            return int(data)

        # ----------------------------------------------------
        # Float
        # ----------------------------------------------------

        if tp is float:

            return float(data)

        # ----------------------------------------------------
        # List
        # ----------------------------------------------------

        if cls._is_list(tp):

            item_type = cls._list_type(
                tp
            )

            return [
                cls.from_dict(
                    item,
                    item_type,
                )
                for item in data
            ]

        # ----------------------------------------------------
        # Dictionary
        # ----------------------------------------------------

        if cls._is_dict(tp):

            key_type, value_type = (
                cls._dict_types(tp)
            )

            return {
                cls.from_dict(
                    key,
                    key_type,
                ):
                cls.from_dict(
                    value,
                    value_type,
                )
                for key, value in data.items()
            }

        # ----------------------------------------------------
        # Dataclass
        # ----------------------------------------------------

        if (
            isinstance(tp, type)
            and is_dataclass(tp)
        ):

            try:

                hints = get_type_hints(
                    tp
                )

            except Exception:

                hints = {}

            kwargs = {}

            for f in fields(tp):

                field_type = hints.get(
                    f.name,
                    f.type,
                )

                if f.name in data:

                    kwargs[f.name] = (
                        cls.from_dict(
                            data[f.name],
                            field_type,
                        )
                    )

            return tp(
                **kwargs
            )

        return data

    # ========================================================
    # JSON File
    # ========================================================

    @classmethod
    def load_json(
        cls,
        filename: str,
        target_type,
    ):

        with open(
            filename,
            "r",
            encoding="utf-8",
        ) as f:

            data = json.load(f)

        return cls.from_dict(
            data,
            target_type,
        )

    # --------------------------------------------------------

    @classmethod
    def dump_json(
        cls,
        filename: str,
        value,
        *,
        indent=4,
    ):

        with open(
            filename,
            "w",
            encoding="utf-8",
        ) as f:

            json.dump(
                cls.to_dict(value),
                f,
                ensure_ascii=False,
                indent=indent,
            )


# ============================================================
# Convenience
# ============================================================


def pack(value):
    return MemoryPack.pack(value)


def unpack(data, target_type):
    return MemoryPack.unpack(
        data,
        target_type,
    )


def load(filename, target_type):
    return MemoryPack.load(
        filename,
        target_type,
    )


def dump(filename, value):
    return MemoryPack.dump(
        filename,
        value,
    )


def to_dict(value):
    return MemoryPack.to_dict(
        value
    )