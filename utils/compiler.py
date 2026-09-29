"""Compiler will parse CSharp dump file to convert to python callable code."""

import os
import re
import subprocess
from enum import Enum

from utils.structure import EnumMember, EnumType, Property, StructTable
from utils.console import notice
from utils.util import TemplateString, Utils


class DataSize(Enum):
    bool = 1
    byte = 1
    ubyte = 1
    short = 2
    ushort = 2
    int = 4
    uint = 4
    long = 8
    ulong = 8
    float = 4
    double = 8
    string = 4
    struct = 4


class DataFlag(Enum):
    bool = "Bool"
    byte = "Int8"
    sbyte = "Int8"
    ubyte = "Uint8"
    short = "Int16"
    ushort = "Uint16"
    int = "Int32"
    uint = "Uint32"
    long = "Int64"
    ulong = "Uint64"
    float = "Float32"
    double = "Float64"


class ConvertFlag(Enum):
    short = "convert_short"
    ushort = "convert_ushort"
    int = "convert_int"
    uint = "convert_uint"
    long = "convert_long"
    ulong = "convert_ulong"
    float = "convert_float"
    double = "convert_double"
    string = "convert_string"


class String:
    INDENT = "    "
    NEWLINE = "\n"
    ENUM_CLASS = TemplateString("class %s:")
    VARIABLE_ASSIGNMENT = TemplateString("%s = %s")
    FUNCTION_DEFINE = TemplateString("def %s(%s)%s:")
    WRAPPER_BASE = """from enum import IntEnum
from utils.encryption import convert_short, convert_ushort, convert_int, convert_long, convert_float, convert_double, convert_string, convert_uint, convert_ulong, create_key
import inspect

def dump_table(table_instance) -> list:
    excel_name = table_instance.__class__.__name__.removesuffix("Table")
    module_parts = table_instance.__class__.__module__.split(".")
    source = "ExcelDB" if "ExcelDB" in module_parts else "Excel"
    current_module = inspect.getmodule(inspect.currentframe())
    dump_func = getattr(current_module, f"dump_{source}_{excel_name}")
    password = create_key(excel_name.removesuffix("Excel"))
    return [dump_func(table_instance.DataList(j), password) for j in range(table_instance.DataListLength())]

"""
    WRAPPER_GETTER = TemplateString("excel_instance.%s()")
    WRAPPER_LIST_GETTER = TemplateString("excel_instance.%s(j)")
    WRAPPER_LIST_CONVERTION = TemplateString("%s for j in range(excel_instance.%sLength())")
    WRAPPER_PASSWD_CONVERTION = TemplateString("%s(%s, password)")
    WRAPPER_ENUM_CONVERTION = TemplateString("%s(%s).name")
    WRAPPER_PROP_KV = TemplateString('"%s": %s,\n')
    WRAPPER_LIST_KV = TemplateString('"%s": [%s],\n')
    WRAPPER_FUNC = TemplateString("""
def dump_%s(excel_instance, password: bytes = b"") -> dict:
    return {
%s    }
""")
    WRAPPER_INT_ENUM = TemplateString("class %s(IntEnum):")
    LOCAL_IMPORT = TemplateString("from .%s import %s")


class Re:
    struct = re.compile(
        r"""
        public\s+struct\s+(\w+)\s*:\s*FlatBuffers\.IFlatbufferObject[^{]*\{
        ((?:[^{}]|\{[^{}]*\})*)
        \}
        """,
        re.M | re.S | re.X,
    )
    struct_root = re.compile(
        r"""
        public\s+static\s+([\w\.]+)\s+GetRootAs\w+\s*\(
        """,
        re.M | re.S | re.X,
    )
    struct_property = re.compile(
        r"""
        ^\s*public\s+(?:virtual\s+)?(?:FlatData\.)?
        ([\w\.\[\]]+)\s+(\w+)\s*\{\s*get;\s*\}
        """,
        re.M | re.S | re.X,
    )
    enum = re.compile(
        r"""
        public\s+enum\s+(\w+)[^{]*\{
        ((?:[^{}]|\{[^{}]*\})*)
        \}
        """,
        re.M | re.S | re.X,
    )
    enum_member = re.compile(
        r"""
        public\s+static\s+const\s+FlatData\.\w+\s+(\w+);
        """,
        re.M | re.S | re.X,
    )
    table_data_type = re.compile(
        r"""
        public\s+(?:FlatData\.)?(\w+)\s+DataList\(int\s+j\)
        """,
        re.M | re.S | re.X,
    )


class CSParser:
    TYPE_MAP = {
        "System.Int64": "long",
        "System.UInt64": "ulong",
        "System.Int32": "int",
        "System.UInt32": "uint",
        "System.Int16": "short",
        "System.UInt16": "ushort",
        "System.Single": "float",
        "System.Double": "double",
        "System.Boolean": "bool",
        "System.String": "string",
    }

    def __init__(self, file_path: str) -> None:
        with open(file_path, "rt", encoding="utf8") as file:
            self.data = file.read()
        start_idx = self.data.find("namespace FlatData")
        if start_idx == -1:
            self.flatdata_part = ""
            return
        brace_idx = self.data.find("{", start_idx)
        if brace_idx == -1:
            self.flatdata_part = ""
            return
        index = brace_idx
        open_braces = 1
        while index < len(self.data) - 1 and open_braces:
            index += 1
            if self.data[index] == "{":
                open_braces += 1
            elif self.data[index] == "}":
                open_braces -= 1
        self.flatdata_part = self.data[start_idx:index + 1]

    @classmethod
    def __convert_type(cls, prop_type: str) -> str:
        prop_type = prop_type.removeprefix("FlatData.")
        prop_type = cls.TYPE_MAP.get(prop_type, prop_type)
        return prop_type.removeprefix("Nullable<").removesuffix(">")

    def __parse_struct_property(self, prop_type: str, prop_name: str, prop_data: str) -> Property:
        prop_type = self.__convert_type(prop_type)
        if prop_name.endswith("Length"):
            list_name = prop_name.removesuffix("Length")
            match = re.search(
                rf"""
                public\s+(?:virtual\s+)?(?:FlatData\.)?
                ([\w\.\[\]]+)\s+{list_name}\(int\s+j\)
                """,
                prop_data,
                re.M | re.S | re.X,
            )
            if match:
                return Property(self.__convert_type(match.group(1)), list_name, True)
        is_list = prop_type.endswith("[]")
        return Property(prop_type.removesuffix("[]") if is_list else prop_type, prop_name, is_list)

    def parse_enum(self) -> list[EnumType]:
        used_enum = {
            prop.data_type
            for struct in self.parse_struct()
            for prop in struct.properties
            if prop.data_type not in DataFlag.__members__
        }
        enums = []
        tactic_entity_type_values = {
            "None": 0,
            "Student": 1,
            "Minion": 2,
            "Elite": 4,
            "Champion": 8,
            "Boss": 16,
            "Obstacle": 32,
            "Servant": 64,
            "Vehicle": 128,
            "Summoned": 256,
            "Hallucination": 512,
            "DestructibleProjectile": 1024,
        }
        for enum_name, content in Re.enum.findall(self.data):
            if enum_name not in used_enum:
                continue
            member_names = Re.enum_member.findall(content)
            if not member_names:
                continue
            if enum_name == "TacticEntityType":
                members = [EnumMember(name, str(value)) for name, value in tactic_entity_type_values.items()]
            else:
                members = [EnumMember(member, str(index)) for index, member in enumerate(member_names)]
            enums.append(EnumType(enum_name, "int", members))
        return enums

    def parse_struct(self) -> list[StructTable]:
        structs = []
        for struct_name, struct_data in Re.struct.findall(self.data):
            root_match = Re.struct_root.search(struct_data)
            if not root_match:
                continue
            root_type = root_match.group(1)
            if root_type.startswith("FlatData."):
                source = "Excel"
            elif root_type.startswith("MX.Data.Excel."):
                source = "ExcelDB"
            else:
                continue
            properties = []
            for prop in Re.struct_property.finditer(struct_data):
                prop_type, prop_name = prop.group(1), prop.group(2)
                if "ByteBuffer" in prop_name:
                    continue
                item = self.__parse_struct_property(prop_type, prop_name, struct_data)
                if item:
                    properties.append(item)
            if properties:
                structs.append(StructTable(struct_name, properties, source))
        structs = [struct for struct in structs if not struct.name.endswith("ExcelTable")]
        for struct in tuple(structs):
            if struct.name.endswith("Excel"):
                structs.append(StructTable(
                    struct.name + "Table",
                    [Property(struct.name, "DataList", True)],
                    struct.source,
                ))
        return structs


class CompileToPython:
    DUMP_WRAPPER_NAME = "dump_wrapper"

    def __init__(self, enums: list[EnumType], structs: list[StructTable], extract_dir: str) -> None:
        self.enums = enums
        self.structs = structs
        self.extract_dir = extract_dir
        self.excel_dir = os.path.join(extract_dir, "Excel")
        self.excel_db_dir = os.path.join(extract_dir, "ExcelDB")
        self.enums_by_name = {enum.name: enum for enum in enums}
        self.structs_by_name = {(struct.source, struct.name): struct for struct in structs}

    def __get_struct(self, source: str, name: str) -> StructTable | None:
        return self.structs_by_name.get((source, name))

    @staticmethod
    def __get_source_name(struct: StructTable) -> str:
        return f"{struct.source}_{Utils.convert_name_to_available(struct.name)}"

    def __type_in_struct_or_num(self, prop_type: str, source: str) -> StructTable | EnumType | None:
        enum = self.enums_by_name.get(prop_type)
        if enum:
            return enum if enum.underlying_type in DataFlag.__members__ else None
        return self.__get_struct(source, prop_type)

    def create_enum_files(self) -> None:
        os.makedirs(self.extract_dir, exist_ok=True)
        for enum in self.enums:
            enum_name = Utils.convert_name_to_available(enum.name)
            path = os.path.join(self.extract_dir, f"{enum_name}.py")
            with open(path, "wt", encoding="utf8") as file:
                file.write(String.ENUM_CLASS(enum_name) + "\n")
                for member in enum.members:
                    value = int(member.value) if enum.underlying_type == "int" else member.value
                    file.write(String.INDENT + String.VARIABLE_ASSIGNMENT(
                        Utils.convert_name_to_available(member.name), value
                    ) + "\n")

    def create_fbs_file(self) -> None:
        os.makedirs(self.extract_dir, exist_ok=True)
        os.makedirs(self.excel_dir, exist_ok=True)
        os.makedirs(self.excel_db_dir, exist_ok=True)
        enum_path = os.path.join(self.extract_dir, "enums.fbs")
        with open(enum_path, "wt", encoding="utf8") as file:
            for enum in self.enums:
                file.write(f"enum {enum.name}:int {{\n")
                file.writelines(f"    {member.name} = {member.value},\n" for member in enum.members)
                file.write("}\n\n")
        for source, output_dir in (("Excel", self.excel_dir), ("ExcelDB", self.excel_db_dir)):
            structs = [struct for struct in self.structs if struct.source == source]
            if not structs:
                continue
            path = os.path.join(output_dir, "flatdata.fbs")
            generated_types = set()
            with open(path, "wt", encoding="utf8") as file:
                if self.enums:
                    file.write('include "../enums.fbs";\n\n')
                for struct in structs:
                    if struct.name.endswith("Table") or struct.name in generated_types:
                        continue
                    generated_types.add(struct.name)
                    file.write(f"table {struct.name} {{\n")
                    used_names = set()
                    for prop in struct.properties:
                        fbs_type = self.__convert_fbs_type(prop, source)
                        if fbs_type is None:
                            continue
                        field_name = self.__fbs_field_name(struct.name, prop.name, used_names)
                        used_names.add(field_name)
                        file.write(f"    {field_name}:{fbs_type};\n")
                    file.write("}\n\n")
                for struct in structs:
                    if not struct.name.endswith("Table") or struct.name in generated_types:
                        continue
                    generated_types.add(struct.name)
                    excel_type = struct.properties[0].data_type
                    file.write(f"table {struct.name} {{\n    data_list:[{excel_type}];\n}}\n\n")
                roots = [struct.name for struct in structs if struct.name.endswith("Table") and struct.name in generated_types]
                if roots:
                    file.write(f"root_type {roots[0]};\n")

    def compile_fbs(self) -> None:
        for output_dir in (self.excel_dir, self.excel_db_dir):
            fbs = os.path.join(output_dir, "flatdata.fbs")
            if not os.path.isfile(fbs):
                continue
            subprocess.run(["flatc", "--python", "-I", self.extract_dir, "-o", output_dir, fbs], check=True)
            self.__fix_flatc_imports(output_dir)

    def __fix_flatc_imports(self, output_dir: str) -> None:
        package = os.path.basename(os.path.normpath(output_dir))
        root_package = os.path.basename(os.path.normpath(self.extract_dir))
        root_modules = {
            os.path.splitext(filename)[0]
            for filename in os.listdir(self.extract_dir)
            if filename.endswith(".py") and filename != "__init__.py"
        }
        modules = {
            os.path.splitext(filename)[0]
            for filename in os.listdir(output_dir)
            if filename.endswith(".py") and filename != "__init__.py"
        }
        from_pattern = re.compile(r"^(\s*)from\s+([A-Za-z_]\w*)\s+import\s+(.+?)\s*$", re.M)
        import_pattern = re.compile(r"^(\s*)import\s+([A-Za-z_]\w*)(\s+as\s+[A-Za-z_]\w+)?\s*$", re.M)
        for filename in os.listdir(output_dir):
            if not filename.endswith(".py"):
                continue
            path = os.path.join(output_dir, filename)
            with open(path, "r", encoding="utf8") as file:
                content = file.read()
            original = content

            def replace_from(match: re.Match) -> str:
                indent, module, names = match.groups()
                if module in modules and module != package:
                    return f"{indent}from {root_package}.{package}.{module} import {names}"
                if module in root_modules:
                    return f"{indent}from {root_package}.{module} import {names}"
                return match.group(0)

            def replace_import(match: re.Match) -> str:
                indent, module, alias = match.groups()
                if module in modules and module != package:
                    return f"{indent}from {root_package}.{package} import {module}{alias or ''}"
                if module in root_modules:
                    return f"{indent}from {root_package} import {module}{alias or ''}"
                return match.group(0)

            content = from_pattern.sub(replace_from, content)
            content = import_pattern.sub(replace_import, content)
            if content != original:
                with open(path, "w", encoding="utf8") as file:
                    file.write(content)

    def create_module_file(self) -> None:
        os.makedirs(self.extract_dir, exist_ok=True)
        os.makedirs(self.excel_dir, exist_ok=True)
        os.makedirs(self.excel_db_dir, exist_ok=True)
        with open(os.path.join(self.extract_dir, "__init__.py"), "wt", encoding="utf8") as file:
            for enum in self.enums:
                name = Utils.convert_name_to_available(enum.name)
                file.write(f"from .{name} import {name}\n")
        for output_dir, source in ((self.excel_dir, "Excel"), (self.excel_db_dir, "ExcelDB")):
            with open(os.path.join(output_dir, "__init__.py"), "wt", encoding="utf8") as file:
                for struct in self.structs:
                    if struct.source == source:
                        name = Utils.convert_name_to_available(struct.name)
                        file.write(f"from .{name} import {name}\n")

    def __wrap_value(self, prop: Property, p_name: str, getter: str, is_list: bool = False, source: str = "Excel") -> str:
        if prop.data_type in ConvertFlag.__members__:
            conversion = String.WRAPPER_PASSWD_CONVERTION(ConvertFlag[prop.data_type].value, getter)
        elif prop.data_type == "bool":
            conversion = f"bool({getter})"
        elif data := self.__type_in_struct_or_num(prop.data_type, source):
            if isinstance(data, StructTable):
                conversion = String.WRAPPER_PASSWD_CONVERTION(self.__get_source_name(data), getter)
            else:
                conversion = String.WRAPPER_ENUM_CONVERTION(
                    Utils.convert_name_to_available(data.name),
                    String.WRAPPER_PASSWD_CONVERTION(
                        ConvertFlag[data.underlying_type].value,
                        getter,
                    ),
                )
        else:
            conversion = getter
        if is_list:
            return String.WRAPPER_LIST_KV(p_name, String.WRAPPER_LIST_CONVERTION(conversion, p_name))
        return String.WRAPPER_PROP_KV(p_name, conversion)

    def __wrap_prop(self, prop: Property, p_name: str, source: str) -> str:
        getter = String.WRAPPER_LIST_GETTER(p_name) if prop.is_list else String.WRAPPER_GETTER(p_name)
        return self.__wrap_value(prop, p_name, getter, prop.is_list, source)

    def create_dump_dict_file(self) -> None:
        path = os.path.join(self.extract_dir, f"{self.DUMP_WRAPPER_NAME}.py")
        generated_types = set()
        with open(path, "wt", encoding="utf8") as file:
            file.write(String.WRAPPER_BASE)
            for enum in self.enums:
                enum_name = Utils.convert_name_to_available(enum.name)
                if enum_name in generated_types:
                    continue
                generated_types.add(enum_name)
                file.write(String.WRAPPER_INT_ENUM(enum_name) + "\n")
                if enum.underlying_type != "int":
                    notice(f"No implementation found for enum type: {enum.underlying_type}.")
                for member in enum.members:
                    file.write(String.INDENT + String.VARIABLE_ASSIGNMENT(
                        Utils.convert_name_to_available(member.name), member.value
                    ) + "\n")
                file.write("\n")
            for struct in self.structs:
                dump_name = self.__get_source_name(struct)
                if dump_name in generated_types:
                    continue
                generated_types.add(dump_name)
                items = "".join(
                    String.INDENT * 2
                    + self.__wrap_prop(prop, Utils.convert_name_to_available(prop.name), struct.source)
                    for prop in struct.properties
                )
                file.write(String.WRAPPER_FUNC(dump_name, items))

    def create_repack_dict_file(self) -> None:
        wrapper_base = """import flatbuffers
from utils.encryption import xor, create_key, convert_short, convert_ushort, convert_int, convert_uint, convert_long, convert_ulong, encrypt_float, encrypt_double, encrypt_string
from . import *
from . import Excel, ExcelDB
"""
        path = os.path.join(self.extract_dir, "repack_wrapper.py")
        with open(path, "wt", encoding="utf8") as file:
            file.write(wrapper_base + "\n")
            for struct in self.structs:
                self.__write_repack_struct(file, struct)

    def __get_repack_name(self, struct: StructTable) -> str:
        return self.__get_source_name(struct)

    def __write_repack_struct(self, file, struct: StructTable) -> None:
        struct_name = Utils.convert_name_to_available(struct.name)
        pack_name = self.__get_repack_name(struct)
        module_name = struct.source
        if struct_name.endswith("ExcelTable"):
            record_type = struct_name[:-5]
            record_struct = self.__get_struct(struct.source, record_type)
            record_pack_name = self.__get_repack_name(record_struct) if record_struct else f"{struct.source}_{record_type}"
            file.write(
                f"def pack_{pack_name}(builder: flatbuffers.Builder, dump_list: list, encrypt=True) -> int:\n"
                f"    offsets = [pack_{record_pack_name}(builder, record, encrypt) for record in dump_list]\n"
                f"    {module_name}.{struct_name}.StartDataListVector(builder, len(offsets))\n"
                f"    for offset in reversed(offsets):\n"
                f"        builder.PrependUOffsetTRelative(offset)\n"
                f"    data_list = builder.EndVector()\n"
                f"    {module_name}.{struct_name}.Start(builder)\n"
                f"    {module_name}.{struct_name}.AddDataList(builder, data_list)\n"
                f"    return {module_name}.{struct_name}.End(builder)\n\n"
            )
            return
        password_key = struct.name.removesuffix("Excel")
        file.write(
            f"def pack_{pack_name}(builder: flatbuffers.Builder, data: dict, encrypt=True) -> int:\n"
            f'    password = create_key("{password_key}") if encrypt else None\n'
        )
        for prop in struct.properties:
            if prop.is_list:
                self.__write_repack_vector(file, struct, prop)
            elif prop.data_type == "string":
                file.write(
                    f"    {prop.name}_off = builder.CreateString("
                    f"encrypt_string(data.get('{prop.name}', ''), password))\n"
                )
            else:
                conversion, _ = self._get_conversion_code(prop, f"data.get('{prop.name}', 0)")
                file.write(f"    {prop.name}_val = {conversion}\n")
        file.write(f"    {module_name}.{struct_name}.Start(builder)\n")
        for prop in struct.properties:
            value = f"{prop.name}_vec" if prop.is_list else f"{prop.name}_off" if prop.data_type == "string" else f"{prop.name}_val"
            field_name = self.__fbs_field_name(struct.name, prop.name, set())
            file.write(f"    {module_name}.{struct_name}.Add{Utils.convert_name_to_available(field_name).title().replace('_', '')}(builder, {value})\n")
        file.write(f"    return {module_name}.{struct_name}.End(builder)\n\n")

    def __write_repack_vector(self, file, struct: StructTable, prop: Property) -> None:
        module_name = struct.source
        struct_name = Utils.convert_name_to_available(struct.name)
        name = prop.name
        data_type = prop.data_type
        file.write(f"    {name}_vec = 0\n    if '{name}' in data:\n        {name}_items = data['{name}']\n")
        if data_type == "string":
            file.write(
                f"        {name}_offsets = [builder.CreateString(encrypt_string(item, password)) for item in {name}_items]\n"
                f"        {module_name}.{struct_name}.Start{name.title().replace('_', '')}Vector(builder, len({name}_offsets))\n"
                f"        for offset in reversed({name}_offsets):\n"
                f"            builder.PrependUOffsetTRelative(offset)\n"
            )
        elif child := self.__get_struct(struct.source, data_type):
            child_name = self.__get_repack_name(child)
            file.write(
                f"        {name}_offsets = [pack_{child_name}(builder, item, encrypt) for item in {name}_items]\n"
                f"        {module_name}.{struct_name}.Start{name.title().replace('_', '')}Vector(builder, len({name}_offsets))\n"
                f"        for offset in reversed({name}_offsets):\n"
                f"            builder.PrependUOffsetTRelative(offset)\n"
            )
        else:
            flag = DataFlag.__members__.get(data_type, DataFlag.int).value
            file.write(
                f"        {module_name}.{struct_name}.Start{name.title().replace('_', '')}Vector(builder, len({name}_items))\n"
                f"        for item in reversed({name}_items):\n"
                f"            builder.Prepend{flag}({self._get_conversion_code(prop, 'item')[0]})\n"
            )
        file.write(f"        {name}_vec = builder.EndVector()\n")

    def _get_conversion_code(self, prop: Property, value_var: str) -> tuple[str, str]:
        data_type = prop.data_type
        if data_type == "bool":
            return value_var, data_type
        if data_type in self.enums_by_name:
            return f"convert_int({value_var}, password)", "int"
        if data_type == "float":
            return f"encrypt_float({value_var}, password)", data_type
        if data_type == "double":
            return f"encrypt_double({value_var}, password)", data_type
        func = {
            "short": "convert_short",
            "ushort": "convert_ushort",
            "int": "convert_int",
            "uint": "convert_uint",
            "long": "convert_long",
            "ulong": "convert_ulong",
        }.get(data_type, "convert_int")
        return f"{func}({value_var}, password)", data_type

    @staticmethod
    def __to_snake_case(name: str) -> str:
        return re.sub(r"_+", "_", re.sub(r"[^a-z0-9_]", "_", re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower())).strip("_")

    def __fbs_field_name(self, struct_name: str, field_name: str, used_names: set) -> str:
        name = self.__to_snake_case(field_name) or "field"
        if name[0].isdigit():
            name = "_" + name
        if name == self.__to_snake_case(struct_name):
            name += "_value"
        if name in used_names:
            index = 1
            while f"{name}_{index}" in used_names:
                index += 1
            name = f"{name}_{index}"
        return name

    def __convert_fbs_type(self, prop: Property, source: str) -> str | None:
        mapping = {
            "bool": "bool",
            "byte": "byte",
            "ubyte": "ubyte",
            "short": "short",
            "ushort": "ushort",
            "int": "int",
            "uint": "uint",
            "long": "long",
            "ulong": "ulong",
            "float": "float",
            "double": "double",
            "string": "string",
        }
        data_type = prop.data_type
        if data_type in mapping:
            fbs_type = mapping[data_type]
        elif data_type in self.enums_by_name:
            fbs_type = data_type
        elif self.__get_struct(source, data_type):
            fbs_type = data_type
        else:
            return None
        return f"[{fbs_type}]" if prop.is_list else fbs_type
