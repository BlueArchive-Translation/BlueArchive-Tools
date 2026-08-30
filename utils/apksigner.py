import os
import re
import sys
import base64
import struct
import shutil
import hashlib
import tempfile
import subprocess
from pathlib import Path


class ApkSigner:
    V2_ID = 0x7109871A
    V3_ID = 0xF05368C0
    MAGIC = b"APK Sig Block 42"
    CHUNK_SIZE = 1024 * 1024
    RSA_SHA256 = 0x0103
    EC_SHA256 = 0x0201

    JAVA_SOURCE = r'''
import java.io.*;
import java.nio.file.*;
import java.security.Key;
import java.security.KeyStore;
import java.security.PrivateKey;
import java.security.PublicKey;
import java.security.Signature;
import java.security.cert.Certificate;
import java.util.Base64;

public class ApkJksHelper {
    static KeyStore load(String path, String storePass) throws Exception {
        KeyStore ks = KeyStore.getInstance("JKS");
        try (InputStream in = new FileInputStream(path)) {
            ks.load(in, storePass.toCharArray());
        }
        return ks;
    }

    static void info(String path, String storePass, String alias) throws Exception {
        KeyStore ks = load(path, storePass);
        Certificate cert = ks.getCertificate(alias);

        if (cert == null) {
            throw new RuntimeException("找不到证书: " + alias);
        }

        PublicKey publicKey = cert.getPublicKey();

        System.out.println("TYPE=" + publicKey.getAlgorithm());
        System.out.println("CERT=" + Base64.getEncoder().encodeToString(cert.getEncoded()));
        System.out.println("PUB=" + Base64.getEncoder().encodeToString(publicKey.getEncoded()));
    }

    static void sign(
        String path,
        String storePass,
        String keyPass,
        String alias,
        String algorithm,
        String dataPath
    ) throws Exception {
        KeyStore ks = load(path, storePass);

        Key key = ks.getKey(
            alias,
            keyPass.toCharArray()
        );

        if (!(key instanceof PrivateKey)) {
            throw new RuntimeException("JKS 条目不是 PrivateKeyEntry");
        }

        byte[] data = Files.readAllBytes(
            Paths.get(dataPath)
        );

        Signature signature = Signature.getInstance(
            algorithm
        );

        signature.initSign(
            (PrivateKey) key
        );

        signature.update(data);

        byte[] result = signature.sign();

        System.out.println(
            Base64.getEncoder().encodeToString(result)
        );
    }

    public static void main(String[] args) throws Exception {
        if (args.length < 1) {
            throw new RuntimeException("缺少操作类型");
        }

        if (args[0].equals("info")) {
            if (args.length != 4) {
                throw new RuntimeException("info 参数错误");
            }

            info(
                args[1],
                args[2],
                args[3]
            );

            return;
        }

        if (args[0].equals("sign")) {
            if (args.length != 7) {
                throw new RuntimeException("sign 参数错误");
            }

            sign(
                args[1],
                args[2],
                args[3],
                args[4],
                args[5],
                args[6]
            );

            return;
        }

        throw new RuntimeException(
            "未知操作: " + args[0]
        );
    }
}
'''

    def __init__(
        self,
        apk_path,
        jks_path,
        ks_pass,
        key_pass,
        output_path,
        alias=None,
        min_sdk=28,
        max_sdk=0x7FFFFFFF,
        apksigner_path=None
    ):
        self.apk_path = Path(apk_path).expanduser().resolve()
        self.jks_path = Path(jks_path).expanduser().resolve()
        self.output_path = Path(output_path).expanduser().resolve()
        self.ks_pass = ks_pass
        self.key_pass = key_pass
        self.alias = alias
        self.min_sdk = min_sdk
        self.max_sdk = max_sdk

        if not self.apk_path.is_file():
            raise FileNotFoundError(
                f"找不到 APK: {self.apk_path}"
            )

        if not self.jks_path.is_file():
            raise FileNotFoundError(
                f"找不到 JKS: {self.jks_path}"
            )

        self.java = self._find_command("java")
        self.javac = self._find_command("javac")
        self.keytool = self._find_command("keytool")

        if not self.java:
            raise RuntimeError("找不到 java")

        if not self.javac:
            raise RuntimeError("找不到 javac")

        if not self.keytool:
            raise RuntimeError("找不到 keytool")

        if apksigner_path:
            self.apksigner = str(
                Path(apksigner_path)
                .expanduser()
                .resolve()
            )

            if not Path(self.apksigner).is_file():
                raise FileNotFoundError(
                    f"找不到 apksigner: {self.apksigner}"
                )
        else:
            self.apksigner = self._find_apksigner()

    def _run(self, command, check=True):
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace"
        )

        if check and result.returncode != 0:
            raise RuntimeError(
                result.stdout.strip()
            )

        return result.stdout

    def _find_command(self, name):
        result = shutil.which(name)

        if result:
            return result

        if os.name == "nt":
            for ext in (
                ".exe",
                ".cmd",
                ".bat"
            ):
                result = shutil.which(
                    name + ext
                )

                if result:
                    return result

        return None

    def _find_apksigner(self):
        direct = self._find_command(
            "apksigner"
        )

        if direct:
            return direct

        roots = []

        for env_name in (
            "ANDROID_HOME",
            "ANDROID_SDK_ROOT"
        ):
            value = os.environ.get(
                env_name
            )

            if value:
                roots.append(
                    Path(value)
                )

        if os.name == "nt":
            local = os.environ.get(
                "LOCALAPPDATA"
            )

            if local:
                roots.append(
                    Path(local) /
                    "Android" /
                    "Sdk"
                )

        roots.append(
            Path.home() /
            "Android" /
            "Sdk"
        )

        candidates = []

        for root in roots:
            build_tools = (
                root /
                "build-tools"
            )

            if not build_tools.is_dir():
                continue

            for version in build_tools.iterdir():
                if not version.is_dir():
                    continue

                filename = (
                    "apksigner.bat"
                    if os.name == "nt"
                    else "apksigner"
                )

                path = version / filename

                if path.is_file():
                    candidates.append(
                        (
                            version.name,
                            path
                        )
                    )

        if not candidates:
            return None

        def version_key(item):
            return tuple(
                int(x)
                if x.isdigit()
                else 0
                for x in re.split(
                    r"[._-]",
                    item[0]
                )
            )

        candidates.sort(
            key=version_key,
            reverse=True
        )

        return str(
            candidates[0][1]
        )

    def _compile_helper(self, directory):
        source = (
            directory /
            "ApkJksHelper.java"
        )

        classes = (
            directory /
            "classes"
        )

        classes.mkdir()

        source.write_text(
            self.JAVA_SOURCE,
            encoding="utf-8"
        )

        self._run([
            self.javac,
            "-encoding",
            "UTF-8",
            "-d",
            str(classes),
            str(source)
        ])

        return classes

    def _java_info(self, classes):
        output = self._run([
            self.java,
            "-cp",
            str(classes),
            "ApkJksHelper",
            "info",
            str(self.jks_path),
            self.ks_pass,
            self.alias
        ])

        result = {}

        for line in output.splitlines():
            if "=" not in line:
                continue

            key, value = line.split(
                "=",
                1
            )

            result[key] = value.strip()

        return result

    def _java_sign(
        self,
        classes,
        data,
        algorithm
    ):
        digest = hashlib.sha256(
            data
        ).hexdigest()

        data_file = (
            classes.parent /
            f"sign-{digest}.bin"
        )

        data_file.write_bytes(
            data
        )

        try:
            output = self._run([
                self.java,
                "-cp",
                str(classes),
                "ApkJksHelper",
                "sign",
                str(self.jks_path),
                self.ks_pass,
                self.key_pass,
                self.alias,
                algorithm,
                str(data_file)
            ])

            lines = [
                x.strip()
                for x in output.splitlines()
                if x.strip()
            ]

            if not lines:
                raise RuntimeError(
                    "Java 没有返回签名"
                )

            return base64.b64decode(
                lines[-1]
            )

        finally:
            try:
                data_file.unlink()
            except OSError:
                pass

    @staticmethod
    def _u32(value):
        return struct.pack(
            "<I",
            value
        )

    @staticmethod
    def _u64(value):
        return struct.pack(
            "<Q",
            value
        )

    @classmethod
    def _lp(cls, data):
        return (
            cls._u32(len(data)) +
            data
        )

    @staticmethod
    def _find_eocd(data):
        signature = b"PK\x05\x06"

        start = max(
            0,
            len(data) - 65557
        )

        position = data.rfind(
            signature,
            start
        )

        if position < 0:
            raise RuntimeError(
                "找不到 ZIP End of Central Directory"
            )

        if position + 22 > len(data):
            raise RuntimeError(
                "EOCD 数据损坏"
            )

        comment_length = struct.unpack_from(
            "<H",
            data,
            position + 20
        )[0]

        if (
            position +
            22 +
            comment_length
            != len(data)
        ):
            raise RuntimeError(
                "EOCD 不是 APK 最后结构"
            )

        return position

    @classmethod
    def _get_zip_info(cls, data):
        eocd = cls._find_eocd(data)

        cd_size = struct.unpack_from(
            "<I",
            data,
            eocd + 12
        )[0]

        cd_offset = struct.unpack_from(
            "<I",
            data,
            eocd + 16
        )[0]

        if (
            cd_size == 0xFFFFFFFF or
            cd_offset == 0xFFFFFFFF
        ):
            raise RuntimeError(
                "不支持 ZIP64 APK"
            )

        if (
            cd_offset +
            cd_size
            != eocd
        ):
            raise RuntimeError(
                "ZIP Central Directory 位置异常"
            )

        return (
            cd_offset,
            cd_size,
            eocd
        )

    @classmethod
    def _find_signing_block(
        cls,
        data,
        cd_offset
    ):
        if cd_offset < 24:
            return None

        footer_offset = (
            cd_offset - 24
        )

        footer = data[
            footer_offset:
            cd_offset
        ]

        if (
            len(footer) != 24 or
            footer[8:24] != cls.MAGIC
        ):
            return None

        size = struct.unpack_from(
            "<Q",
            footer,
            0
        )[0]

        block_start = (
            cd_offset -
            24 -
            size
        )

        if block_start < 0:
            raise RuntimeError(
                "APK Signing Block 越界"
            )

        first_size = struct.unpack_from(
            "<Q",
            data,
            block_start
        )[0]

        if first_size != size:
            raise RuntimeError(
                "APK Signing Block size 不一致"
            )

        return block_start

    @classmethod
    def _remove_old_signing_block(
        cls,
        data
    ):
        cd_offset, _, eocd = (
            cls._get_zip_info(data)
        )

        block_start = (
            cls._find_signing_block(
                data,
                cd_offset
            )
        )

        if block_start is None:
            return data

        block_length = (
            cd_offset -
            block_start
        )

        result = (
            data[:block_start] +
            data[cd_offset:]
        )

        new_eocd = (
            eocd -
            block_length
        )

        struct.pack_into(
            "<I",
            result,
            new_eocd + 16,
            block_start
        )

        return result

    @classmethod
    def _chunked_digest(cls, parts):
        chunk_digests = []

        for part in parts:
            offset = 0

            while offset < len(part):
                chunk = part[
                    offset:
                    offset + cls.CHUNK_SIZE
                ]

                chunk_digests.append(
                    hashlib.sha256(
                        b"\xa5" +
                        cls._u32(
                            len(chunk)
                        ) +
                        chunk
                    ).digest()
                )

                offset += len(chunk)

        return hashlib.sha256(
            b"\x5a" +
            cls._u32(
                len(chunk_digests)
            ) +
            b"".join(chunk_digests)
        ).digest()

    @classmethod
    def _make_v2_signed_data(
        cls,
        digest,
        certificate,
        algorithm_id
    ):
        digest_record = (
            cls._u32(algorithm_id) +
            cls._lp(digest)
        )

        digests = cls._lp(
            cls._lp(
                digest_record
            )
        )

        certificates = cls._lp(
            cls._lp(
                certificate
            )
        )

        additional_attributes = cls._lp(
            b""
        )

        return (
            digests +
            certificates +
            additional_attributes
        )

    @classmethod
    def _make_v3_signed_data(
        cls,
        digest,
        certificate,
        algorithm_id,
        min_sdk,
        max_sdk
    ):
        digest_record = (
            cls._u32(algorithm_id) +
            cls._lp(digest)
        )

        digests = cls._lp(
            cls._lp(
                digest_record
            )
        )

        certificates = cls._lp(
            cls._lp(
                certificate
            )
        )

        additional_attributes = cls._lp(
            b""
        )

        return (
            digests +
            certificates +
            cls._u32(min_sdk) +
            cls._u32(max_sdk) +
            additional_attributes
        )

    @classmethod
    def _make_v2_signer(
        cls,
        signed_data,
        signature,
        public_key,
        algorithm_id
    ):
        signature_record = (
            cls._u32(algorithm_id) +
            cls._lp(signature)
        )

        signatures = cls._lp(
            cls._lp(
                signature_record
            )
        )

        signer = (
            cls._lp(signed_data) +
            signatures +
            cls._lp(public_key)
        )

        return cls._lp(signer)

    @classmethod
    def _make_v3_signer(
        cls,
        signed_data,
        signature,
        public_key,
        algorithm_id,
        min_sdk,
        max_sdk
    ):
        signature_record = (
            cls._u32(algorithm_id) +
            cls._lp(signature)
        )

        signatures = cls._lp(
            cls._lp(
                signature_record
            )
        )

        signer = (
            cls._lp(signed_data) +
            cls._u32(min_sdk) +
            cls._u32(max_sdk) +
            signatures +
            cls._lp(public_key)
        )

        return cls._lp(signer)

    @classmethod
    def _make_signers(cls, signer):
        return cls._lp(
            signer
        )

    @classmethod
    def _make_pair(
        cls,
        block_id,
        value
    ):
        pair = (
            cls._u32(block_id) +
            value
        )

        return (
            cls._u64(len(pair)) +
            pair
        )

    @classmethod
    def _make_signing_block(
        cls,
        v2_signer,
        v3_signer
    ):
        v2_signers = cls._make_signers(
            v2_signer
        )

        v3_signers = cls._make_signers(
            v3_signer
        )

        pairs = (
            cls._make_pair(
                cls.V2_ID,
                v2_signers
            ) +
            cls._make_pair(
                cls.V3_ID,
                v3_signers
            )
        )

        size = (
            len(pairs) +
            24
        )

        return (
            cls._u64(size) +
            pairs +
            cls._u64(size) +
            cls.MAGIC
        )

    @classmethod
    def _insert_signing_block(
        cls,
        data,
        signing_block
    ):
        cd_offset, _, eocd = (
            cls._get_zip_info(data)
        )

        before_cd = data[
            :cd_offset
        ]

        central_directory = data[
            cd_offset:eocd
        ]

        eocd_data = bytearray(
            data[eocd:]
        )

        new_cd_offset = (
            cd_offset +
            len(signing_block)
        )

        struct.pack_into(
            "<I",
            eocd_data,
            16,
            new_cd_offset
        )

        return (
            before_cd +
            signing_block +
            central_directory +
            bytes(eocd_data)
        )

    @staticmethod
    def _collect_v1(data):
        import zipfile

        result = {}

        with tempfile.NamedTemporaryFile(
            suffix=".apk",
            delete=False
        ) as f:
            temp = Path(f.name)
            f.write(data)

        try:
            with zipfile.ZipFile(
                temp,
                "r"
            ) as z:
                for info in z.infolist():
                    name = info.filename
                    upper = name.upper()

                    if not upper.startswith(
                        "META-INF/"
                    ):
                        continue

                    if (
                        upper.endswith(
                            "MANIFEST.MF"
                        ) or
                        upper.endswith(
                            ".SF"
                        ) or
                        upper.endswith(
                            ".RSA"
                        ) or
                        upper.endswith(
                            ".DSA"
                        ) or
                        upper.endswith(
                            ".EC"
                        )
                    ):
                        result[name] = z.read(
                            info
                        )
        finally:
            try:
                temp.unlink()
            except OSError:
                pass

        return result

    @staticmethod
    def _verify_v1(
        data,
        original
    ):
        import zipfile

        with tempfile.NamedTemporaryFile(
            suffix=".apk",
            delete=False
        ) as f:
            temp = Path(f.name)
            f.write(data)

        try:
            with zipfile.ZipFile(
                temp,
                "r"
            ) as z:
                for name, expected in original.items():
                    actual = z.read(name)

                    if actual != expected:
                        raise RuntimeError(
                            f"V1 文件被修改: {name}"
                        )
        finally:
            try:
                temp.unlink()
            except OSError:
                pass

    def _resolve_alias(self):
        if self.alias:
            return

        output = self._run([
            self.keytool,
            "-list",
            "-v",
            "-keystore",
            str(self.jks_path),
            "-storepass",
            self.ks_pass
        ])

        blocks = re.split(
            r"\n(?=\s*Alias name:)",
            output
        )

        for block in blocks:
            if "PrivateKeyEntry" not in block:
                continue

            match = re.search(
                r"Alias name:\s*(.+)",
                block
            )

            if match:
                self.alias = (
                    match.group(1)
                    .strip()
                )
                return

        raise RuntimeError(
            "JKS 中找不到 PrivateKeyEntry"
        )

    def _verify_apk(self):
        if not self.apksigner:
            print(
                "警告：找不到 apksigner，跳过最终验证"
            )
            return

        result = subprocess.run(
            [
                "java",
                "-jar",
                self.apksigner,
                "verify",
                "--verbose",
                "--print-certs",
                str(self.output_path)
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace"
        )

        print(result.stdout)

        if result.returncode != 0:
            raise RuntimeError(
                "apksigner verify 失败"
            )

    def sign(self):
        print("读取 APK")

        original_data = (
            self.apk_path.read_bytes()
        )

        print("保存原始 V1 数据")

        original_v1 = (
            self._collect_v1(
                original_data
            )
        )

        if not original_v1:
            raise RuntimeError(
                "没有找到原始 V1 签名数据"
            )

        for name, value in original_v1.items():
            print(
                f"保留: {name} ({len(value)} bytes)"
            )

        with tempfile.TemporaryDirectory() as temp:
            classes = self._compile_helper(
                Path(temp)
            )

            print("读取 JKS")

            self._resolve_alias()

            print(
                f"私钥别名: {self.alias}"
            )

            info = self._java_info(
                classes
            )

            key_type = info.get(
                "TYPE"
            )

            if key_type == "RSA":
                algorithm_id = (
                    self.RSA_SHA256
                )
                signature_algorithm = (
                    "SHA256withRSA"
                )
            elif key_type == "EC":
                algorithm_id = (
                    self.EC_SHA256
                )
                signature_algorithm = (
                    "SHA256withECDSA"
                )
            else:
                raise RuntimeError(
                    f"不支持的私钥类型: {key_type}"
                )

            certificate = (
                base64.b64decode(
                    info["CERT"]
                )
            )

            public_key = (
                base64.b64decode(
                    info["PUB"]
                )
            )

            print(
                f"密钥类型: {key_type}"
            )

            print(
                "移除旧 V2/V3 Signing Block"
            )

            unsigned = (
                self._remove_old_signing_block(
                    original_data
                )
            )

            cd_offset, _, eocd = (
                self._get_zip_info(
                    unsigned
                )
            )

            before_cd = unsigned[
                :cd_offset
            ]

            central_directory = unsigned[
                cd_offset:eocd
            ]

            eocd_data = bytearray(
                unsigned[eocd:]
            )

            struct.pack_into(
                "<I",
                eocd_data,
                16,
                cd_offset
            )

            print(
                "计算 V2/V3 Digest"
            )

            digest = (
                self._chunked_digest([
                    before_cd,
                    central_directory,
                    bytes(eocd_data)
                ])
            )

            print(
                f"Digest: {digest.hex()}"
            )

            print(
                "生成 V2 Signed Data"
            )

            v2_signed_data = (
                self._make_v2_signed_data(
                    digest,
                    certificate,
                    algorithm_id
                )
            )

            print(
                "生成 V3 Signed Data"
            )

            v3_signed_data = (
                self._make_v3_signed_data(
                    digest,
                    certificate,
                    algorithm_id,
                    self.min_sdk,
                    self.max_sdk
                )
            )

            print(
                "生成 V2 Signature"
            )

            v2_signature = (
                self._java_sign(
                    classes,
                    v2_signed_data,
                    signature_algorithm
                )
            )

            print(
                "生成 V3 Signature"
            )

            v3_signature = (
                self._java_sign(
                    classes,
                    v3_signed_data,
                    signature_algorithm
                )
            )

            v2_signer = (
                self._make_v2_signer(
                    v2_signed_data,
                    v2_signature,
                    public_key,
                    algorithm_id
                )
            )

            v3_signer = (
                self._make_v3_signer(
                    v3_signed_data,
                    v3_signature,
                    public_key,
                    algorithm_id,
                    self.min_sdk,
                    self.max_sdk
                )
            )

            signing_block = (
                self._make_signing_block(
                    v2_signer,
                    v3_signer
                )
            )

            print(
                f"Signing Block: {len(signing_block)} bytes"
            )

            print(
                "写入 V2/V3 Signing Block"
            )

            final_data = (
                self._insert_signing_block(
                    unsigned,
                    signing_block
                )
            )

            print(
                "检查原始 V1 数据"
            )

            self._verify_v1(
                final_data,
                original_v1
            )

            self.output_path.parent.mkdir(
                parents=True,
                exist_ok=True
            )

            self.output_path.write_bytes(
                final_data
            )

        print()
        print("原始 V1 文件检查通过")
        print(f"输出文件: {self.output_path}")
        print()
        print("验证 V2/V3")

        self._verify_apk()

        print()
        print("签名完成")

        return self.output_path
