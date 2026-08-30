from __future__ import annotations

import os
import re
import hmac
import hashlib
import sqlite3 as _sqlite3
import tempfile
import shutil

from Crypto.Cipher import AES


# ============================================================
# 常量
# ============================================================

SQLITE_HEADER = b"SQLite format 3\x00"

FILE_HEADER_SIZE = 16

AES_BLOCK_SIZE = 16

KEY_SIZE = 32

IV_SIZE = 16

HMAC_SALT_MASK = 0x3A

FAST_KDF_ITER = 2


# ============================================================
# CipherConfig
# ============================================================

class CipherConfig:

    def __init__(
        self,
        name,
        page_size,
        kdf_iter,
        kdf_hash,
        hmac_hash,
        reserve_size,
        padding_size=0,
    ):

        self.name = name
        self.page_size = page_size
        self.kdf_iter = kdf_iter
        self.kdf_hash = kdf_hash
        self.hmac_hash = hmac_hash
        self.reserve_size = reserve_size
        self.padding_size = padding_size

        self.iv_size = IV_SIZE
        self.key_size = KEY_SIZE

        self.hmac_size = hashlib.new(
            hmac_hash
        ).digest_size

    @property
    def encrypted_page_size(self):
        """
        普通页面可加密的数据长度。

        SQLCipher 4:
            4096 - 80 = 4016

        SQLCipher 3:
            1024 - 48 = 976
        """

        return (
            self.page_size
            - self.reserve_size
        )

    @property
    def page1_encrypted_size(self):
        """
        Page 1 因为前 16 bytes 是 salt，
        所以实际加密区域少 16 bytes。
        """

        return (
            self.page_size
            - self.reserve_size
            - FILE_HEADER_SIZE
        )


# ============================================================
# SQLCipher 3
# ============================================================

SQLCIPHER3 = CipherConfig(
    name="SQLCipher 3",
    page_size=1024,
    kdf_iter=64000,
    kdf_hash="sha1",
    hmac_hash="sha1",
    reserve_size=48,
    padding_size=12,
)


# ============================================================
# SQLCipher 4
# ============================================================

SQLCIPHER4 = CipherConfig(
    name="SQLCipher 4",
    page_size=4096,
    kdf_iter=256000,
    kdf_hash="sha512",
    hmac_hash="sha512",
    reserve_size=80,
    padding_size=0,
)


# ============================================================
# Key 解析
# ============================================================

def _normalize_key(value):

    if isinstance(value, bytes):
        return value

    if isinstance(value, bytearray):
        return bytes(value)

    value = str(value).strip()

    value = value.rstrip(";").strip()

    # --------------------------------------------------------
    # 外层引号
    # --------------------------------------------------------

    if (
        len(value) >= 2
        and value[0] == value[-1]
        and value[0] in ("'", '"')
    ):

        value = value[1:-1].strip()

    # --------------------------------------------------------
    # x'0123456789abcdef'
    # --------------------------------------------------------

    match = re.fullmatch(
        r"x'([0-9a-fA-F]+)'",
        value,
        re.I,
    )

    if match:

        hex_string = match.group(1)

        if len(hex_string) % 2 != 0:

            raise ValueError(
                "十六进制 key 长度必须为偶数"
            )

        return bytes.fromhex(
            hex_string
        )

    # --------------------------------------------------------
    # x"0123456789abcdef"
    # --------------------------------------------------------

    match = re.fullmatch(
        r'x"([0-9a-fA-F]+)"',
        value,
        re.I,
    )

    if match:

        hex_string = match.group(1)

        if len(hex_string) % 2 != 0:

            raise ValueError(
                "十六进制 key 长度必须为偶数"
            )

        return bytes.fromhex(
            hex_string
        )

    # --------------------------------------------------------
    # 普通字符串
    # --------------------------------------------------------

    if (
        len(value) >= 2
        and value[0] == value[-1]
        and value[0] in ("'", '"')
    ):

        value = value[1:-1]

    return value.encode("utf-8")


# ============================================================
# PBKDF2
# ============================================================

def _pbkdf2(
    password,
    salt,
    iterations,
    hash_name,
):

    return hashlib.pbkdf2_hmac(
        hash_name,
        password,
        salt,
        iterations,
        KEY_SIZE,
    )


# ============================================================
# SQLCipher Codec
# ============================================================

class SQLCipherCodec:

    def __init__(
        self,
        filename,
        key,
    ):

        self.filename = os.path.abspath(
            filename
        )

        self.key = _normalize_key(
            key
        )

        self.config = None

        self.salt = None

        self.encryption_key = None
        self.hmac_key = None

        self.raw_key = False

    # ========================================================
    # password key
    # ========================================================

    def _derive_password_keys(
        self,
        config,
    ):

        print(
            "[SQLCipher] "
            f"主 KDF: PBKDF2-{config.kdf_hash.upper()}, "
            f"iterations={config.kdf_iter}"
        )

        self.encryption_key = _pbkdf2(
            self.key,
            self.salt,
            config.kdf_iter,
            config.kdf_hash,
        )

        self._derive_hmac_key(
            config
        )

    # ========================================================
    # raw key
    # ========================================================

    def _derive_raw_keys(
        self,
        config,
    ):

        if len(self.key) != KEY_SIZE:

            raise ValueError(
                "raw key 必须为 32 bytes，"
                f"当前为 {len(self.key)} bytes"
            )

        print(
            "[SQLCipher] "
            "raw key = 32 bytes"
        )

        self.encryption_key = self.key

        self._derive_hmac_key(
            config
        )

    # ========================================================
    # HMAC key
    # ========================================================

    def _derive_hmac_key(
        self,
        config,
    ):

        hmac_salt = bytes(
            b ^ HMAC_SALT_MASK
            for b in self.salt
        )

        print(
            "[SQLCipher] "
            "HMAC KDF: PBKDF2-"
            f"{config.hmac_hash.upper()}, "
            f"iterations={FAST_KDF_ITER}"
        )

        self.hmac_key = _pbkdf2(
            self.encryption_key,
            hmac_salt,
            FAST_KDF_ITER,
            config.hmac_hash,
        )

    # ========================================================
    # HMAC
    # ========================================================

    def _calculate_hmac(
        self,
        ciphertext,
        iv,
        page_number,
        config,
    ):

        page_number_bytes = (
            page_number.to_bytes(
                4,
                byteorder="little",
                signed=False,
            )
        )

        message = (
            ciphertext
            + iv
            + page_number_bytes
        )

        return hmac.new(
            self.hmac_key,
            message,
            config.hmac_hash,
        ).digest()

    # ========================================================
    # 解密页面
    # ========================================================

    def decrypt_page(
        self,
        page,
        page_number,
    ):

        config = self.config

        if config is None:

            raise RuntimeError(
                "SQLCipherCodec.config 尚未设置"
            )

        page_size = config.page_size
        reserve_size = config.reserve_size
        iv_size = config.iv_size
        hmac_size = config.hmac_size
        padding_size = config.padding_size

        if len(page) != page_size:

            raise ValueError(
                f"page={page_number}: "
                f"页面长度={len(page)}, "
                f"期望={page_size}"
            )

        # ====================================================
        # SQLCipher 3
        #
        # page 1:
        #
        # salt 16
        # ciphertext 960
        # IV 16
        # HMAC 20
        # padding 12
        #
        # 16 + 960 + 16 + 20 + 12 = 1024
        #
        # page 2+:
        #
        # ciphertext 976
        # IV 16
        # HMAC 20
        # padding 12
        #
        # ====================================================

        # ====================================================
        # SQLCipher 4
        #
        # page 1:
        #
        # salt 16
        # ciphertext 4000
        # IV 16
        # HMAC 64
        #
        # page 2+:
        #
        # ciphertext 4016
        # IV 16
        # HMAC 64
        #
        # ====================================================

        if page_number == 1:

            if page[:16] != self.salt:

                raise ValueError(
                    "page=1: salt 不匹配\n"
                    f"expected={self.salt.hex()}\n"
                    f"actual={page[:16].hex()}"
                )

            ciphertext_start = FILE_HEADER_SIZE

            ciphertext_size = (
                page_size
                - reserve_size
                - FILE_HEADER_SIZE
            )

        else:

            ciphertext_start = 0

            ciphertext_size = (
                page_size
                - reserve_size
            )

        ciphertext_end = (
            ciphertext_start
            + ciphertext_size
        )

        iv_start = ciphertext_end

        iv_end = (
            iv_start
            + iv_size
        )

        hmac_start = iv_end

        hmac_end = (
            hmac_start
            + hmac_size
        )

        padding_start = hmac_end

        padding_end = (
            padding_start
            + padding_size
        )

        if padding_end != page_size:

            raise ValueError(
                f"page={page_number}: "
                "页面布局错误\n"
                f"page_size={page_size}\n"
                f"ciphertext_start={ciphertext_start}\n"
                f"ciphertext_size={ciphertext_size}\n"
                f"iv_start={iv_start}\n"
                f"iv_size={iv_size}\n"
                f"hmac_start={hmac_start}\n"
                f"hmac_size={hmac_size}\n"
                f"padding_start={padding_start}\n"
                f"padding_size={padding_size}\n"
                f"padding_end={padding_end}"
            )

        ciphertext = page[
            ciphertext_start:
            ciphertext_end
        ]

        iv = page[
            iv_start:
            iv_end
        ]

        stored_hmac = page[
            hmac_start:
            hmac_end
        ]

        # ====================================================
        # AES block
        # ====================================================

        if (
            len(ciphertext)
            % AES_BLOCK_SIZE
            != 0
        ):

            raise ValueError(
                f"page={page_number}: "
                f"ciphertext 长度 "
                f"{len(ciphertext)} "
                "不是 16 的整数倍"
            )

        # ====================================================
        # HMAC
        # ====================================================

        expected_hmac = self._calculate_hmac(
            ciphertext,
            iv,
            page_number,
            config,
        )

        if not hmac.compare_digest(
            stored_hmac,
            expected_hmac,
        ):

            raise ValueError(
                f"page={page_number}: "
                "HMAC 校验失败\n"
                f"stored={stored_hmac.hex()}\n"
                f"expected={expected_hmac.hex()}"
            )

        # ====================================================
        # AES
        # ====================================================

        cipher = AES.new(
            self.encryption_key,
            AES.MODE_CBC,
            iv,
        )

        plaintext = cipher.decrypt(
            ciphertext
        )

        # ====================================================
        # 恢复 SQLite 页面
        # ====================================================

        if page_number == 1:

            result = (
                SQLITE_HEADER
                + plaintext
                + b"\x00" * reserve_size
            )

        else:

            result = (
                plaintext
                + b"\x00" * reserve_size
            )

        if len(result) != page_size:

            raise ValueError(
                f"page={page_number}: "
                f"恢复后页面长度="
                f"{len(result)}, "
                f"期望={page_size}"
            )

        return result

    # ========================================================
    # 加密页面
    # ========================================================

    def encrypt_page(
        self,
        page,
        page_number,
    ):

        config = self.config

        if config is None:

            raise RuntimeError(
                "SQLCipherCodec.config 尚未设置"
            )

        page_size = config.page_size
        reserve_size = config.reserve_size
        iv_size = config.iv_size
        hmac_size = config.hmac_size
        padding_size = config.padding_size

        if len(page) != page_size:

            raise ValueError(
                f"page={page_number}: "
                f"页面长度={len(page)}, "
                f"期望={page_size}"
            )

        # ====================================================
        # Page 1
        # ====================================================

        if page_number == 1:

            if page[:16] != SQLITE_HEADER:

                raise ValueError(
                    "page=1: "
                    "SQLite header 不正确"
                )

            plaintext = page[
                FILE_HEADER_SIZE:
                FILE_HEADER_SIZE
                + config.page1_encrypted_size
            ]

            prefix = self.salt

        # ====================================================
        # Page 2+
        # ====================================================

        else:

            plaintext = page[
                :config.encrypted_page_size
            ]

            prefix = b""

        if (
            len(plaintext)
            % AES_BLOCK_SIZE
            != 0
        ):

            raise ValueError(
                f"page={page_number}: "
                f"加密数据长度={len(plaintext)} "
                "不是 AES block 的整数倍"
            )

        # ====================================================
        # 新 IV
        # ====================================================

        iv = os.urandom(
            iv_size
        )

        cipher = AES.new(
            self.encryption_key,
            AES.MODE_CBC,
            iv,
        )

        ciphertext = cipher.encrypt(
            plaintext
        )

        # ====================================================
        # HMAC
        # ====================================================

        page_hmac = self._calculate_hmac(
            ciphertext,
            iv,
            page_number,
            config,
        )

        # ====================================================
        # SQLCipher 3 padding
        # ====================================================

        if padding_size:

            padding = os.urandom(
                padding_size
            )

        else:

            padding = b""

        result = (
            prefix
            + ciphertext
            + iv
            + page_hmac
            + padding
        )

        if len(result) != page_size:

            raise ValueError(
                f"page={page_number}: "
                f"加密后页面长度={len(result)}, "
                f"期望={page_size}"
            )

        return result

    # ========================================================
    # 检查数据库
    # ========================================================

    def _check_file_size(
        self,
        config,
    ):

        file_size = os.path.getsize(
            self.filename
        )

        if (
            file_size == 0
            or file_size % config.page_size != 0
        ):

            raise ValueError(
                f"文件大小 {file_size} "
                f"无法被 page_size "
                f"{config.page_size} 整除"
            )

        return file_size

    # ========================================================
    # 尝试配置
    # ========================================================

    def _try_config(
        self,
        config,
        raw_key=False,
    ):

        file_size = self._check_file_size(
            config
        )

        print(
            "[SQLCipher] "
            f"文件大小={file_size}"
        )

        print(
            "[SQLCipher] "
            f"page_size={config.page_size}"
        )

        print(
            "[SQLCipher] "
            f"reserve_size={config.reserve_size}"
        )

        with open(
            self.filename,
            "rb",
        ) as f:

            first_page = f.read(
                config.page_size
            )

        if len(first_page) != config.page_size:

            raise ValueError(
                "第一页读取不完整"
            )

        self.salt = first_page[:16]

        print(
            "[SQLCipher] "
            f"salt={self.salt.hex()}"
        )

        self.config = config

        self.raw_key = raw_key

        if raw_key:

            self._derive_raw_keys(
                config
            )

        else:

            self._derive_password_keys(
                config
            )

        plaintext = self.decrypt_page(
            first_page,
            1,
        )

        magic = plaintext[:16]

        print(
            "[SQLCipher] "
            f"解密后的前16字节="
            f"{magic!r}"
        )

        if magic != SQLITE_HEADER:

            raise ValueError(
                "SQLite header 不正确"
            )

        # ----------------------------------------------------
        # 检查 SQLite header 中的 page size
        # ----------------------------------------------------

        header_page_size = int.from_bytes(
            plaintext[16:18],
            byteorder="big",
        )

        if header_page_size == 1:

            header_page_size = 65536

        if header_page_size != config.page_size:

            raise ValueError(
                "SQLite page size 不匹配: "
                f"header={header_page_size}, "
                f"config={config.page_size}"
            )

        return True

    # ========================================================
    # 自动检测
    # ========================================================

    def detect(self):

        errors = []

        configs = [
            SQLCIPHER4,
            SQLCIPHER3,
        ]

        print()
        print("=" * 60)
        print(
            "[SQLCipher] 开始自动检测"
        )
        print("=" * 60)

        print(
            "[SQLCipher] "
            f"输入 key 长度={len(self.key)} bytes"
        )

        # ====================================================
        # Raw key
        # ====================================================

        if len(self.key) == KEY_SIZE:

            for config in configs:

                print()
                print("-" * 60)

                print(
                    "[SQLCipher] "
                    f"尝试 {config.name} raw key..."
                )

                try:

                    self._try_config(
                        config,
                        raw_key=True,
                    )

                    print(
                        "[SQLCipher] "
                        f"检测成功: "
                        f"{config.name} raw key"
                    )

                    return config

                except Exception as e:

                    error = (
                        f"{config.name} raw key: "
                        f"{type(e).__name__}: {e}"
                    )

                    errors.append(error)

                    print(
                        "[SQLCipher] "
                        f"失败:\n{error}"
                    )

        else:

            print(
                "[SQLCipher] "
                "key 不是 32 bytes，"
                "跳过 raw key"
            )

        # ====================================================
        # Password
        # ====================================================

        for config in configs:

            print()
            print("-" * 60)

            print(
                "[SQLCipher] "
                f"尝试 {config.name} password..."
            )

            try:

                self._try_config(
                    config,
                    raw_key=False,
                )

                print(
                    "[SQLCipher] "
                    f"检测成功: "
                    f"{config.name} password"
                )

                return config

            except Exception as e:

                error = (
                    f"{config.name} password: "
                    f"{type(e).__name__}: {e}"
                )

                errors.append(error)

                print(
                    "[SQLCipher] "
                    f"失败:\n{error}"
                )

        raise ValueError(
            "无法识别 SQLCipher 数据库。\n\n"
            "详细错误:\n"
            + "\n".join(
                "  " + x
                for x in errors
            )
        )

    # ========================================================
    # 解密整个数据库
    # ========================================================

    def decrypt_to(
        self,
        output,
    ):

        if self.config is None:

            self.detect()

        config = self.config

        file_size = self._check_file_size(
            config
        )

        total_pages = (
            file_size
            // config.page_size
        )

        print()
        print(
            "[SQLCipher] "
            f"开始解密 {total_pages} 个页面..."
        )

        with open(
            self.filename,
            "rb",
        ) as src, open(
            output,
            "wb",
        ) as dst:

            for page_number in range(
                1,
                total_pages + 1,
            ):

                page = src.read(
                    config.page_size
                )

                if len(page) != config.page_size:

                    raise ValueError(
                        f"page={page_number}: "
                        "读取不完整"
                    )

                plaintext = self.decrypt_page(
                    page,
                    page_number,
                )

                dst.write(
                    plaintext
                )

                if (
                    page_number == 1
                    or page_number % 1000 == 0
                    or page_number == total_pages
                ):

                    print(
                        "[SQLCipher] "
                        f"page "
                        f"{page_number}/"
                        f"{total_pages}"
                    )

        print(
            "[SQLCipher] "
            "数据库解密完成"
        )

        return output

    # ========================================================
    # 加密整个数据库
    # ========================================================

    def encrypt_to(
        self,
        output,
        salt=None,
    ):

        config = self.config

        if config is None:

            raise RuntimeError(
                "请先 detect()"
            )

        # ----------------------------------------------------
        # 如果没有 salt
        # ----------------------------------------------------

        if salt is not None:

            if len(salt) != FILE_HEADER_SIZE:

                raise ValueError(
                    "salt 必须为 16 bytes"
                )

            self.salt = bytes(salt)

        elif self.salt is None:

            self.salt = os.urandom(
                FILE_HEADER_SIZE
            )

        # ----------------------------------------------------
        # 重新派生 key
        # ----------------------------------------------------

        if self.raw_key:

            self._derive_raw_keys(
                config
            )

        else:

            self._derive_password_keys(
                config
            )

        file_size = os.path.getsize(
            self.filename
        )

        if (
            file_size == 0
            or file_size % config.page_size != 0
        ):

            raise ValueError(
                f"明文 SQLite 文件大小 "
                f"{file_size} 不正确"
            )

        total_pages = (
            file_size
            // config.page_size
        )

        print()
        print(
            "[SQLCipher] "
            f"开始重新加密 "
            f"{total_pages} 个页面..."
        )

        with open(
            self.filename,
            "rb",
        ) as src, open(
            output,
            "wb",
        ) as dst:

            for page_number in range(
                1,
                total_pages + 1,
            ):

                page = src.read(
                    config.page_size
                )

                if len(page) != config.page_size:

                    raise ValueError(
                        f"page={page_number}: "
                        "读取不完整"
                    )

                encrypted = self.encrypt_page(
                    page,
                    page_number,
                )

                dst.write(
                    encrypted
                )

                if (
                    page_number == 1
                    or page_number % 1000 == 0
                    or page_number == total_pages
                ):

                    print(
                        "[SQLCipher] "
                        f"encrypt page "
                        f"{page_number}/"
                        f"{total_pages}"
                    )

        print(
            "[SQLCipher] "
            "数据库重新加密完成"
        )

        return output

    # ========================================================
    # 写回原文件
    # ========================================================

    def encrypt_in_place(
        self,
        plaintext_file,
    ):

        directory = os.path.dirname(
            os.path.abspath(
                self.filename
            )
        )

        temp_encrypted = os.path.join(
            directory,
            ".sqlcipher_encrypted.tmp",
        )

        try:

            self.filename = os.path.abspath(
                plaintext_file
            )

            self.encrypt_to(
                temp_encrypted
            )

            # ------------------------------------------------
            # 验证重新加密后的文件
            # ------------------------------------------------

            verify_codec = SQLCipherCodec(
                temp_encrypted,
                self.key,
            )

            verify_codec.detect()

            print(
                "[SQLCipher] "
                "重新加密文件 HMAC 验证成功"
            )

            # ------------------------------------------------
            # 原子替换
            # ------------------------------------------------

            os.replace(
                temp_encrypted,
                self._original_filename,
            )

            print(
                "[SQLCipher] "
                "已安全写回原数据库"
            )

        finally:

            if os.path.exists(
                temp_encrypted
            ):

                try:
                    os.remove(
                        temp_encrypted
                    )
                except OSError:
                    pass


# ============================================================
# Cursor
# ============================================================

class Cursor:

    def __init__(
        self,
        connection,
        cursor=None,
    ):

        self.connection = connection
        self._cursor = cursor

    # ========================================================
    # execute
    # ========================================================

    def execute(
        self,
        sql,
        parameters=(),
    ):

        sql = str(sql).strip()

        # ----------------------------------------------------
        # PRAGMA key
        # ----------------------------------------------------

        if re.match(
            r"^PRAGMA\s+key\s*=",
            sql,
            re.I,
        ):

            value = re.sub(
                r"^PRAGMA\s+key\s*=\s*",
                "",
                sql,
                flags=re.I,
            ).strip()

            key = _normalize_key(
                value
            )

            print(
                "[SQLCipher] "
                f"收到 key: {len(key)} bytes"
            )

            self.connection._set_key(
                key
            )

            # ========================================================
            # 重要：
            # _set_key() 完成后，重新取得 SQLite cursor
            # ========================================================

            if self.connection._connection is not None:

                self._cursor = (
                    self.connection._connection.cursor()
                )

            return self

        # ----------------------------------------------------
        # 数据库尚未初始化
        # ----------------------------------------------------

        if self._cursor is None:

            raise RuntimeError(
                "数据库尚未初始化。"
                "请先执行 PRAGMA key"
            )

        # ----------------------------------------------------
        # SQLCipher PRAGMA
        # ----------------------------------------------------

        sqlcipher_pragmas = {
            "cipher_page_size",
            "cipher_default_page_size",
            "kdf_iter",
            "cipher_kdf_algorithm",
            "cipher_hmac_algorithm",
            "cipher_use_hmac",
            "cipher_version",
            "cipher_integrity_check",
            "cipher_plaintext_header_size",
            "cipher_hmac_pgno",
            "cipher_hmac_salt_mask",
            "cipher_memory_security",
            "cipher_provider",
        }

        match = re.match(
            r"^PRAGMA\s+([A-Za-z0-9_]+)",
            sql,
            re.I,
        )

        if match:

            pragma_name = (
                match.group(1).lower()
            )

            if pragma_name in sqlcipher_pragmas:

                return self

        # ----------------------------------------------------
        # 普通 SQLite
        # ----------------------------------------------------

        self._cursor.execute(
            sql,
            parameters,
        )

        return self

    # ========================================================
    # executemany
    # ========================================================

    def executemany(
        self,
        sql,
        seq_of_parameters,
    ):

        if self._cursor is None:

            raise RuntimeError(
                "数据库尚未初始化"
            )

        self._cursor.executemany(
            sql,
            seq_of_parameters,
        )

        return self

    # ========================================================
    # executescript
    # ========================================================

    def executescript(
        self,
        script,
    ):

        if self._cursor is None:

            raise RuntimeError(
                "数据库尚未初始化"
            )

        self._cursor.executescript(
            script
        )

        return self

    # ========================================================
    # fetch
    # ========================================================

    def fetchone(self):

        return self._cursor.fetchone()

    def fetchmany(
        self,
        size=None,
    ):

        if size is None:

            return self._cursor.fetchmany()

        return self._cursor.fetchmany(
            size
        )

    def fetchall(self):

        return self._cursor.fetchall()

    # ========================================================
    # properties
    # ========================================================

    @property
    def description(self):

        return self._cursor.description

    @property
    def rowcount(self):

        return self._cursor.rowcount

    @property
    def lastrowid(self):

        return self._cursor.lastrowid

    # ========================================================
    # close
    # ========================================================

    def close(self):

        if self._cursor is not None:

            return self._cursor.close()

    # ========================================================
    # iterator
    # ========================================================

    def __iter__(self):

        return iter(
            self._cursor
        )


# ============================================================
# Connection
# ============================================================

class Connection:

    def __init__(
        self,
        database,
        timeout=5.0,
        detect_types=0,
        isolation_level="",
        check_same_thread=True,
        factory=None,
        cached_statements=128,
        uri=False,
        **kwargs,
    ):

        self.database = os.path.abspath(
            database
        )

        self._original_filename = (
            self.database
        )

        self.timeout = timeout
        self.detect_types = detect_types
        self.isolation_level = isolation_level
        self.check_same_thread = check_same_thread
        self.cached_statements = cached_statements
        self.uri = uri

        self._key = None
        self._cipher = None
        self._connection = None

        self._closed = False

        self._encrypted = False

        # ----------------------------------------------------
        # 用户是否明确要求重新加密
        # ----------------------------------------------------

        self._encrypt_on_close = False

        # ----------------------------------------------------
        # 临时目录
        # ----------------------------------------------------

        self._tmpdir = tempfile.mkdtemp(
            prefix="sqlcipher_"
        )

        self._plaintext = os.path.join(
            self._tmpdir,
            "database.sqlite",
        )

        # ----------------------------------------------------
        # 检查文件
        # ----------------------------------------------------

        if not os.path.exists(
            self.database
        ):

            # 不存在的数据库：
            #
            # 如果之后设置 password，
            # 将创建普通 SQLite，
            # close 时自动加密。
            #
            return

        with open(
            self.database,
            "rb",
        ) as f:

            header = f.read(16)

        # ----------------------------------------------------
        # 普通 SQLite
        # ----------------------------------------------------

        if header == SQLITE_HEADER:

            print(
                "[SQLite] "
                "检测到普通 SQLite 数据库"
            )

            self._plaintext = (
                self.database
            )

            self._connection = (
                _sqlite3.connect(
                    self._plaintext,
                    timeout=timeout,
                    detect_types=detect_types,
                    isolation_level=isolation_level,
                    check_same_thread=check_same_thread,
                    cached_statements=cached_statements,
                    uri=uri,
                )
            )

        else:

            print(
                "[SQLCipher] "
                "检测到疑似加密数据库"
            )

    # ========================================================
    # 设置 key
    # ========================================================

    def _set_key(
        self,
        key,
    ):

        self._key = bytes(key)

        # ----------------------------------------------------
        # 已经打开普通 SQLite
        # ----------------------------------------------------

        if self._connection is not None:

            # ------------------------------------------------
            # 如果用户给普通 SQLite 设置密码，
            # 不需要重新解密。
            #
            # 只需要在 close 时加密。
            # ------------------------------------------------

            self._encrypt_on_close = True

            self._prepare_cipher_for_plaintext()

            return

        # ----------------------------------------------------
        # SQLCipher
        # ----------------------------------------------------

        self._cipher = SQLCipherCodec(
            self._original_filename,
            self._key,
        )

        # ----------------------------------------------------
        # 自动检测
        # ----------------------------------------------------

        self._cipher.detect()

        self._encrypted = True
        self._encrypt_on_close = True

        # ----------------------------------------------------
        # 解密
        # ----------------------------------------------------

        print(
            "[SQLCipher] "
            "正在解密数据库..."
        )

        self._cipher.decrypt_to(
            self._plaintext
        )

        print(
            "[SQLCipher] "
            "解密完成"
        )

        # ----------------------------------------------------
        # 打开 SQLite
        # ----------------------------------------------------

        self._connection = (
            _sqlite3.connect(
                self._plaintext,
                timeout=self.timeout,
                detect_types=self.detect_types,
                isolation_level=self.isolation_level,
                check_same_thread=self.check_same_thread,
                cached_statements=self.cached_statements,
            )
        )

        print(
            "[SQLite] "
            "数据库连接成功"
        )

    # ========================================================
    # 给普通 SQLite 准备加密器
    # ========================================================

    def _prepare_cipher_for_plaintext(self):

        if self._key is None:

            raise RuntimeError(
                "没有设置数据库 key"
            )

        # ----------------------------------------------------
        # 普通 SQLite 使用 SQLCipher 4 作为默认加密格式
        # ----------------------------------------------------

        self._cipher = SQLCipherCodec(
            self._original_filename,
            self._key,
        )

        self._cipher.config = SQLCIPHER4
        self._cipher.raw_key = (
            len(self._key) == KEY_SIZE
        )

        # ----------------------------------------------------
        # 新数据库生成新的 salt
        # ----------------------------------------------------

        self._cipher.salt = os.urandom(
            FILE_HEADER_SIZE
        )

        if self._cipher.raw_key:

            self._cipher._derive_raw_keys(
                SQLCIPHER4
            )

        else:

            self._cipher._derive_password_keys(
                SQLCIPHER4
            )

    # ========================================================
    # cursor
    # ========================================================

    def cursor(self):

        if self._connection is not None:

            return Cursor(
                self,
                self._connection.cursor(),
            )

        return Cursor(
            self,
            None,
        )

    # ========================================================
    # execute
    # ========================================================

    def execute(
        self,
        sql,
        parameters=(),
    ):

        cursor = self.cursor()

        cursor.execute(
            sql,
            parameters,
        )

        return cursor

    # ========================================================
    # executemany
    # ========================================================

    def executemany(
        self,
        sql,
        seq_of_parameters,
    ):

        cursor = self.cursor()

        cursor.executemany(
            sql,
            seq_of_parameters,
        )

        return cursor

    # ========================================================
    # executescript
    # ========================================================

    def executescript(
        self,
        script,
    ):

        cursor = self.cursor()

        cursor.executescript(
            script
        )

        return cursor

    # ========================================================
    # commit
    # ========================================================

    def commit(self):

        if self._connection is not None:

            self._connection.commit()

    # ========================================================
    # rollback
    # ========================================================

    def rollback(self):

        if self._connection is not None:

            self._connection.rollback()

    # ========================================================
    # 加密当前数据库
    # ========================================================

    def encrypt_database(self):

        if self._connection is None:

            raise RuntimeError(
                "数据库尚未打开"
            )

        if self._key is None:

            raise RuntimeError(
                "请先设置 key"
            )

        self._connection.commit()

        # ----------------------------------------------------
        # 如果原来就是 SQLCipher
        # ----------------------------------------------------

        if self._encrypted:

            codec = self._cipher

            plaintext = self._plaintext

            directory = os.path.dirname(
                self._original_filename
            )

            temp_encrypted = os.path.join(
                directory,
                ".sqlcipher_reencrypt.tmp",
            )

            try:

                codec.filename = plaintext

                codec.encrypt_to(
                    temp_encrypted
                )

                # ------------------------------------------------
                # 验证
                # ------------------------------------------------

                verify = SQLCipherCodec(
                    temp_encrypted,
                    self._key,
                )

                verify.detect()

                print(
                    "[SQLCipher] "
                    "写回前验证成功"
                )

                os.replace(
                    temp_encrypted,
                    self._original_filename,
                )

            finally:

                if os.path.exists(
                    temp_encrypted
                ):

                    try:
                        os.remove(
                            temp_encrypted
                        )
                    except OSError:
                        pass

            return

        # ----------------------------------------------------
        # 普通 SQLite -> SQLCipher
        # ----------------------------------------------------

        codec = self._cipher

        codec.filename = self._plaintext

        directory = os.path.dirname(
            self._original_filename
        )

        temp_encrypted = os.path.join(
            directory,
            ".sqlcipher_encrypt.tmp",
        )

        try:

            codec.encrypt_to(
                temp_encrypted
            )

            verify = SQLCipherCodec(
                temp_encrypted,
                self._key,
            )

            verify.detect()

            print(
                "[SQLCipher] "
                "普通 SQLite -> SQLCipher "
                "验证成功"
            )

            # ------------------------------------------------
            # 关闭原 SQLite
            # ------------------------------------------------

            self._connection.close()

            self._connection = None

            os.replace(
                temp_encrypted,
                self._original_filename,
            )

            self._encrypted = True

        finally:

            if os.path.exists(
                temp_encrypted
            ):

                try:
                    os.remove(
                        temp_encrypted
                    )
                except OSError:
                    pass

    # ========================================================
    # close
    # ========================================================

    def close(self):

        if self._closed:

            return

        error = None

        try:

            if self._connection is not None:

                self._connection.commit()

                # ------------------------------------------------
                # 加密数据库：
                #
                # SQLite 临时文件 -> SQLCipher 原文件
                # ------------------------------------------------

                if (
                    self._encrypt_on_close
                    and self._key is not None
                ):

                    self.encrypt_database()

                elif (
                    self._connection is not None
                ):

                    self._connection.close()

                    self._connection = None

        except Exception as e:

            error = e

            try:

                if self._connection is not None:

                    self._connection.rollback()

            except Exception:
                pass

        finally:

            if self._connection is not None:

                try:
                    self._connection.close()
                except Exception:
                    pass

                self._connection = None

            shutil.rmtree(
                self._tmpdir,
                ignore_errors=True,
            )

            self._closed = True

        if error is not None:

            raise error

    # ========================================================
    # context manager
    # ========================================================

    def __enter__(self):

        return self

    def __exit__(
        self,
        exc_type,
        exc_val,
        exc_tb,
    ):

        if exc_type is not None:

            try:
                self.rollback()
            except Exception:
                pass

            # ------------------------------------------------
            # 出现异常时不要写回
            # ------------------------------------------------

            self._encrypt_on_close = False

        self.close()

        return False

    # ========================================================
    # properties
    # ========================================================

    @property
    def in_transaction(self):

        if self._connection is None:

            return False

        return self._connection.in_transaction

    @property
    def total_changes(self):

        if self._connection is None:

            return 0

        return self._connection.total_changes

    @property
    def encrypted(self):

        return self._encrypted

    @property
    def key(self):

        return self._key


# ============================================================
# sqlite3 兼容对象
# ============================================================

class _SQLiteCompat:

    Connection = Connection
    Cursor = Cursor

    Error = _sqlite3.Error
    DatabaseError = _sqlite3.DatabaseError
    IntegrityError = _sqlite3.IntegrityError
    OperationalError = _sqlite3.OperationalError
    ProgrammingError = _sqlite3.ProgrammingError
    InterfaceError = _sqlite3.InterfaceError
    InternalError = _sqlite3.InternalError
    NotSupportedError = _sqlite3.NotSupportedError

    Row = _sqlite3.Row

    PARSE_DECLTYPES = (
        _sqlite3.PARSE_DECLTYPES
    )

    PARSE_COLNAMES = (
        _sqlite3.PARSE_COLNAMES
    )

    def connect(
        self,
        database,
        timeout=5.0,
        detect_types=0,
        isolation_level="",
        check_same_thread=True,
        factory=None,
        cached_statements=128,
        uri=False,
        **kwargs,
    ):

        return Connection(
            database,
            timeout=timeout,
            detect_types=detect_types,
            isolation_level=isolation_level,
            check_same_thread=check_same_thread,
            factory=factory,
            cached_statements=cached_statements,
            uri=uri,
            **kwargs,
        )


# ============================================================
# 对外 sqlite3
# ============================================================

sqlite3 = _SQLiteCompat()


__all__ = [
    "sqlite3",
    "Connection",
    "Cursor",
    "SQLCipherCodec",
    "CipherConfig",
    "SQLCIPHER3",
    "SQLCIPHER4",
]
