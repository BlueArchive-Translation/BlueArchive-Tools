import os
import sys
import stat
import json
import shlex
import shutil
import logging
import subprocess
from pathlib import Path
from typing import Optional, Union, List, Dict, Any


class SSHServerError(Exception):
    pass


class SSHConnectionError(SSHServerError):
    pass


class SSHCommandError(SSHServerError):
    pass


class SSHFileError(SSHServerError):
    pass


class SSHServer:
    def __init__(
        self,
        host: str,
        username: str = "root",
        password: Optional[str] = None,
        port: int = 22,
        private_key: Optional[str] = None,
        connect_timeout: int = 15,
        command_timeout: int = 300,
        strict_host_key_checking: bool = False,
        known_hosts_file: Optional[str] = None,
        log_level: int = logging.INFO
    ):
        self.host = host
        self.username = username
        self.password = password
        self.port = int(port)
        self.private_key = private_key
        self.connect_timeout = int(connect_timeout)
        self.command_timeout = int(command_timeout)
        self.strict_host_key_checking = strict_host_key_checking
        self.known_hosts_file = known_hosts_file

        self.logger = logging.getLogger(f"SSHServer.{self.host}")
        self.logger.setLevel(log_level)

        if not self.logger.handlers:
            handler = logging.StreamHandler()
            formatter = logging.Formatter(
                "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
            )
            handler.setFormatter(formatter)
            self.logger.addHandler(handler)

        self._validate_config()
        self._check_dependencies()

    def _validate_config(self):
        if not self.host:
            raise ValueError("host 不能为空")

        if not self.username:
            raise ValueError("username 不能为空")

        if self.port <= 0 or self.port > 65535:
            raise ValueError("port 必须在 1-65535 之间")

        if self.password is None and self.private_key is None:
            raise ValueError("必须提供 password 或 private_key")

    def _check_dependencies(self):
        required = ["ssh", "scp", "sftp"]

        missing = []

        for command in required:
            if shutil.which(command) is None:
                missing.append(command)

        if missing:
            raise EnvironmentError(
                f"系统缺少必要命令: {', '.join(missing)}"
            )

        if self.password is not None and self.private_key is None:
            if shutil.which("sshpass") is None:
                raise EnvironmentError(
                    "当前使用密码认证，但系统没有安装 sshpass。"
                    "请安装 sshpass，或者改用 SSH 私钥认证。"
                )

    def _target(self) -> str:
        return f"{self.username}@{self.host}"

    def _base_ssh_args(self) -> List[str]:
        args = [
            "ssh",
            "-p",
            str(self.port),
            "-o",
            f"ConnectTimeout={self.connect_timeout}",
            "-o",
            "ServerAliveInterval=30",
            "-o",
            "ServerAliveCountMax=3",
            "-o",
            "LogLevel=ERROR"
        ]

        if self.strict_host_key_checking:
            args.extend([
                "-o",
                "StrictHostKeyChecking=yes"
            ])
        else:
            args.extend([
                "-o",
                "StrictHostKeyChecking=no",
                "-o",
                "UserKnownHostsFile=/dev/null"
            ])

        if self.known_hosts_file:
            args.extend([
                "-o",
                f"UserKnownHostsFile={self.known_hosts_file}"
            ])

        if self.private_key:
            args.extend([
                "-i",
                os.path.expanduser(self.private_key)
            ])

        return args

    def _base_scp_args(self) -> List[str]:
        args = [
            "scp",
            "-P",
            str(self.port),
            "-o",
            f"ConnectTimeout={self.connect_timeout}",
        ]

        if self.strict_host_key_checking:
            args.extend([
                "-o",
                "StrictHostKeyChecking=yes"
            ])
        else:
            args.extend([
                "-o",
                "StrictHostKeyChecking=no",
                "-o",
                "UserKnownHostsFile=/dev/null"
            ])

        if self.known_hosts_file:
            args.extend([
                "-o",
                f"UserKnownHostsFile={self.known_hosts_file}"
            ])

        if self.private_key:
            args.extend([
                "-i",
                self.private_key
            ])

        return args

    def _password_command(self, command: List[str]) -> List[str]:
        if self.password is not None and self.private_key is None:
            return [
                "sshpass",
                "-p",
                self.password
            ] + command

        return command

    def _run(
        self,
        command: List[str],
        timeout: Optional[int] = None,
        check: bool = True,
        capture_output: bool = True,
        input_data: Optional[str] = None
    ) -> subprocess.CompletedProcess:
        timeout = timeout or self.command_timeout

        self.logger.debug(
            "执行命令: %s",
            " ".join(
                shlex.quote(str(x))
                for x in command
                if str(x) != self.password
            )
        )

        try:
            result = subprocess.run(
                command,
                input=input_data,
                text=True,
                stdout=subprocess.PIPE if capture_output else None,
                stderr=subprocess.PIPE if capture_output else None,
                timeout=timeout
            )
        except subprocess.TimeoutExpired as e:
            raise SSHCommandError(
                f"命令执行超时: {timeout} 秒"
            ) from e
        except FileNotFoundError as e:
            raise SSHConnectionError(
                f"系统找不到命令: {command[0]}"
            ) from e
        except OSError as e:
            raise SSHConnectionError(
                f"执行系统命令失败: {e}"
            ) from e

        if check and result.returncode != 0:
            stderr = result.stderr.strip() if result.stderr else ""
            stdout = result.stdout.strip() if result.stdout else ""

            message = stderr or stdout or f"退出码: {result.returncode}"

            raise SSHCommandError(message)

        return result

    def test_connection(self) -> bool:
        command = self._base_ssh_args()
        command.extend([
            self._target(),
            "printf",
            "%s",
            "SSH_OK"
        ])

        command = self._password_command(command)

        result = self._run(
            command,
            timeout=self.connect_timeout
        )

        return result.stdout.strip() == "SSH_OK"

    def execute(
        self,
        command: str,
        timeout: Optional[int] = None,
        cwd: Optional[str] = None,
        environment: Optional[Dict[str, str]] = None
    ) -> str:
        if not command:
            raise ValueError("command 不能为空")

        script = []

        if cwd:
            script.append(
                f"cd {shlex.quote(cwd)}"
            )

        if environment:
            for key, value in environment.items():
                script.append(
                    f"export {shlex.quote(str(key))}="
                    f"{shlex.quote(str(value))}"
                )

        script.append(command)

        remote_script = "\n".join(script) + "\n"

        args = self._base_ssh_args()

        args.extend([
            self._target(),
            "bash",
            "-s"
        ])

        args = self._password_command(args)

        result = self._run(
            args,
            timeout=timeout,
            input_data=remote_script
        )

        return result.stdout

    def execute_json(
        self,
        command: str,
        timeout: Optional[int] = None
    ) -> Any:
        output = self.execute(
            command,
            timeout=timeout
        )

        try:
            return json.loads(output)
        except json.JSONDecodeError as e:
            raise SSHCommandError(
                f"远程命令返回内容不是合法 JSON: {output}"
            ) from e

    def exists(self, remote_path: str) -> bool:
        result = self.execute(
            f"if [ -e {shlex.quote(remote_path)} ]; "
            f"then printf '1'; else printf '0'; fi"
        )

        return result.strip() == "1"

    def is_file(self, remote_path: str) -> bool:
        result = self.execute(
            f"if [ -f {shlex.quote(remote_path)} ]; "
            f"then printf '1'; else printf '0'; fi"
        )

        return result.strip() == "1"

    def is_dir(self, remote_path: str) -> bool:
        result = self.execute(
            f"if [ -d {shlex.quote(remote_path)} ]; "
            f"then printf '1'; else printf '0'; fi"
        )

        return result.strip() == "1"

    def get_file_size(self, remote_path: str) -> int:
        command = (
            f"if [ ! -f {shlex.quote(remote_path)} ]; then "
            f"exit 2; "
            f"fi; "
            f"stat -c '%s' {shlex.quote(remote_path)}"
        )

        result = self.execute(command)

        try:
            return int(result.strip())
        except ValueError as e:
            raise SSHFileError(
                f"无法解析文件大小: {remote_path}"
            ) from e

    def get_file_mtime(self, remote_path: str) -> int:
        command = (
            f"if [ ! -e {shlex.quote(remote_path)} ]; then "
            f"exit 2; "
            f"fi; "
            f"stat -c '%Y' {shlex.quote(remote_path)}"
        )

        result = self.execute(command)

        try:
            return int(result.strip())
        except ValueError as e:
            raise SSHFileError(
                f"无法解析文件修改时间: {remote_path}"
            ) from e

    def get_file_mode(self, remote_path: str) -> int:
        command = (
            f"if [ ! -e {shlex.quote(remote_path)} ]; then "
            f"exit 2; "
            f"fi; "
            f"stat -c '%a' {shlex.quote(remote_path)}"
        )

        result = self.execute(command)

        try:
            return int(result.strip(), 8)
        except ValueError as e:
            raise SSHFileError(
                f"无法解析文件权限: {remote_path}"
            ) from e

    def get_owner(self, remote_path: str) -> str:
        command = (
            f"if [ ! -e {shlex.quote(remote_path)} ]; then "
            f"exit 2; "
            f"fi; "
            f"stat -c '%U' {shlex.quote(remote_path)}"
        )

        return self.execute(command).strip()

    def get_group(self, remote_path: str) -> str:
        command = (
            f"if [ ! -e {shlex.quote(remote_path)} ]; then "
            f"exit 2; "
            f"fi; "
            f"stat -c '%G' {shlex.quote(remote_path)}"
        )

        return self.execute(command).strip()

    def get_file_info(self, remote_path: str) -> Dict[str, Any]:
        command = (
            f"if [ ! -e {shlex.quote(remote_path)} ]; then "
            f"exit 2; "
            f"fi; "
            f"stat -c '%n|%F|%s|%Y|%a|%U|%G' "
            f"{shlex.quote(remote_path)}"
        )

        result = self.execute(command).strip()

        parts = result.split("|", 6)

        if len(parts) != 7:
            raise SSHFileError(
                f"无法解析文件信息: {remote_path}"
            )

        return {
            "path": parts[0],
            "type": parts[1],
            "size": int(parts[2]),
            "mtime": int(parts[3]),
            "mode": int(parts[4], 8),
            "owner": parts[5],
            "group": parts[6]
        }

    def list_dir(
        self,
        remote_path: str = ".",
        include_hidden: bool = True,
        recursive: bool = False
    ) -> List[Dict[str, Any]]:
        path = shlex.quote(remote_path)

        if recursive:
            if include_hidden:
                command = (
                    f"find {path} -mindepth 1 "
                    f"-printf '%p\\t%y\\t%s\\t%T@\\t%m\\t%u\\t%g\\n'"
                )
            else:
                command = (
                    f"find {path} -mindepth 1 "
                    f"! -name '.*' "
                    f"-printf '%p\\t%y\\t%s\\t%T@\\t%m\\t%u\\t%g\\n'"
                )
        else:
            if include_hidden:
                command = (
                    f"find {path} -mindepth 1 -maxdepth 1 "
                    f"-printf '%p\\t%y\\t%s\\t%T@\\t%m\\t%u\\t%g\\n'"
                )
            else:
                command = (
                    f"find {path} -mindepth 1 -maxdepth 1 "
                    f"! -name '.*' "
                    f"-printf '%p\\t%y\\t%s\\t%T@\\t%m\\t%u\\t%g\\n'"
                )

        output = self.execute(command)

        result = []

        for line in output.splitlines():
            if not line.strip():
                continue

            parts = line.split("\t", 6)

            if len(parts) != 7:
                continue

            file_type = parts[1]

            if file_type == "f":
                item_type = "file"
            elif file_type == "d":
                item_type = "directory"
            elif file_type == "l":
                item_type = "symlink"
            else:
                item_type = "other"

            result.append({
                "name": os.path.basename(parts[0]),
                "path": parts[0],
                "type": item_type,
                "size": int(parts[2]),
                "mtime": int(float(parts[3])),
                "mode": int(parts[4], 8),
                "owner": parts[5],
                "group": parts[6]
            })

        return result

    def list_files(
        self,
        remote_path: str = ".",
        include_hidden: bool = True,
        recursive: bool = False
    ) -> List[Dict[str, Any]]:
        return [
            item
            for item in self.list_dir(
                remote_path,
                include_hidden=include_hidden,
                recursive=recursive
            )
            if item["type"] == "file"
        ]

    def list_directories(
        self,
        remote_path: str = ".",
        include_hidden: bool = True,
        recursive: bool = False
    ) -> List[Dict[str, Any]]:
        return [
            item
            for item in self.list_dir(
                remote_path,
                include_hidden=include_hidden,
                recursive=recursive
            )
            if item["type"] == "directory"
        ]

    def mkdir(
        self,
        remote_path: str,
        parents: bool = True,
        mode: Optional[int] = None
    ):
        path = shlex.quote(remote_path)

        command = "mkdir "

        if parents:
            command += "-p "

        if mode is not None:
            command += f"-m {oct(mode)[2:]} "

        command += path

        self.execute(command)

    def remove(
        self,
        remote_path: str,
        recursive: bool = False,
        force: bool = False
    ):
        path = shlex.quote(remote_path)

        command = "rm "

        if recursive:
            command += "-r "

        if force:
            command += "-f "

        command += path

        self.execute(command)

    def remove_file(self, remote_path: str):
        if self.is_dir(remote_path):
            raise SSHFileError(
                f"目标是目录，不能使用 remove_file: {remote_path}"
            )

        self.remove(remote_path)

    def remove_dir(self, remote_path: str):
        if not self.is_dir(remote_path):
            raise SSHFileError(
                f"目标不是目录: {remote_path}"
            )

        self.remove(
            remote_path,
            recursive=True
        )

    def rename(
        self,
        remote_source: str,
        remote_target: str
    ):
        self.execute(
            f"mv "
            f"{shlex.quote(remote_source)} "
            f"{shlex.quote(remote_target)}"
        )

    def move(
        self,
        remote_source: str,
        remote_target: str
    ):
        self.rename(
            remote_source,
            remote_target
        )

    def copy(
        self,
        remote_source: str,
        remote_target: str,
        recursive: bool = False
    ):
        command = "cp "

        if recursive:
            command += "-r "

        command += (
            f"{shlex.quote(remote_source)} "
            f"{shlex.quote(remote_target)}"
        )

        self.execute(command)

    def chmod(
        self,
        remote_path: str,
        mode: Union[int, str],
        recursive: bool = False
    ):
        if isinstance(mode, int):
            mode_string = oct(mode)[2:]
        else:
            mode_string = str(mode)

        command = "chmod "

        if recursive:
            command += "-R "

        command += (
            f"{shlex.quote(mode_string)} "
            f"{shlex.quote(remote_path)}"
        )

        self.execute(command)

    def chown(
        self,
        remote_path: str,
        owner: str,
        group: Optional[str] = None,
        recursive: bool = False
    ):
        value = owner

        if group:
            value += f":{group}"

        command = "chown "

        if recursive:
            command += "-R "

        command += (
            f"{shlex.quote(value)} "
            f"{shlex.quote(remote_path)}"
        )

        self.execute(command)

    def disk_usage(
        self,
        remote_path: str = "/"
    ) -> Dict[str, int]:
        command = (
            f"df -B1 --output=size,used,avail "
            f"{shlex.quote(remote_path)} | tail -n 1"
        )

        result = self.execute(command).strip()

        parts = result.split()

        if len(parts) != 3:
            raise SSHCommandError(
                f"无法解析磁盘空间信息: {result}"
            )

        return {
            "total": int(parts[0]),
            "used": int(parts[1]),
            "available": int(parts[2])
        }

    def upload(
        self,
        local_path: Union[str, Path],
        remote_path: str,
        recursive: bool = False,
        create_parent: bool = True
    ):
        local_path = str(Path(local_path).expanduser())

        if not os.path.exists(local_path):
            raise FileNotFoundError(
                f"本地文件不存在: {local_path}"
            )

        if os.path.isdir(local_path) and not recursive:
            raise SSHFileError(
                "上传目录必须设置 recursive=True"
            )

        if create_parent:
            remote_parent = remote_path

            if recursive and os.path.isdir(local_path):
                remote_parent = remote_path

            else:
                remote_parent = os.path.dirname(remote_path)

            if remote_parent:
                self.mkdir(
                    remote_parent,
                    parents=True
                )

        args = self._base_scp_args()

        if recursive:
            args.append("-r")

        args.extend([
            local_path,
            f"{self._target()}:{remote_path}"
        ])

        args = self._password_command(args)

        self._run(
            args,
            timeout=self.command_timeout
        )

    def upload_file(
        self,
        local_file: Union[str, Path],
        remote_file: str,
        create_parent: bool = True
    ):
        self.upload(
            local_file,
            remote_file,
            recursive=False,
            create_parent=create_parent
        )

    def upload_directory(
        self,
        local_directory: Union[str, Path],
        remote_directory: str,
        create_parent: bool = True
    ):
        self.upload(
            local_directory,
            remote_directory,
            recursive=True,
            create_parent=create_parent
        )

    def download(
        self,
        remote_path: str,
        local_path: Union[str, Path],
        recursive: bool = False,
        create_parent: bool = True
    ):
        local_path = str(Path(local_path).expanduser())

        if create_parent:
            parent = os.path.dirname(
                os.path.abspath(local_path)
            )

            if parent:
                os.makedirs(
                    parent,
                    exist_ok=True
                )

        if not self.exists(remote_path):
            raise FileNotFoundError(
                f"远程文件不存在: {remote_path}"
            )

        if self.is_dir(remote_path) and not recursive:
            raise SSHFileError(
                "下载目录必须设置 recursive=True"
            )

        args = self._base_scp_args()

        if recursive:
            args.append("-r")

        args.extend([
            f"{self._target()}:{remote_path}",
            local_path
        ])

        args = self._password_command(args)

        self._run(
            args,
            timeout=self.command_timeout
        )

    def download_file(
        self,
        remote_file: str,
        local_file: Union[str, Path],
        create_parent: bool = True
    ):
        self.download(
            remote_file,
            local_file,
            recursive=False,
            create_parent=create_parent
        )

    def download_directory(
        self,
        remote_directory: str,
        local_directory: Union[str, Path],
        create_parent: bool = True
    ):
        self.download(
            remote_directory,
            local_directory,
            recursive=True,
            create_parent=create_parent
        )

    def upload_many(
        self,
        files: List[Union[str, Path]],
        remote_directory: str
    ):
        self.mkdir(
            remote_directory,
            parents=True
        )

        for file_path in files:
            file_path = Path(file_path)

            if not file_path.exists():
                raise FileNotFoundError(
                    f"本地文件不存在: {file_path}"
                )

            remote_path = (
                remote_directory.rstrip("/")
                + "/"
                + file_path.name
            )

            self.upload_file(
                file_path,
                remote_path,
                create_parent=False
            )

    def download_many(
        self,
        remote_files: List[str],
        local_directory: Union[str, Path]
    ):
        local_directory = Path(
            local_directory
        ).expanduser()

        local_directory.mkdir(
            parents=True,
            exist_ok=True
        )

        for remote_file in remote_files:
            filename = os.path.basename(
                remote_file.rstrip("/")
            )

            local_file = local_directory / filename

            self.download_file(
                remote_file,
                local_file,
                create_parent=False
            )

    def read_file(
        self,
        remote_path: str,
        encoding: str = "utf-8"
    ) -> str:
        command = (
            f"cat {shlex.quote(remote_path)}"
        )

        return self.execute(command)

    def write_file(
        self,
        remote_path: str,
        content: str,
        encoding: str = "utf-8",
        create_parent: bool = True
    ):
        if create_parent:
            parent = os.path.dirname(remote_path)

            if parent:
                self.mkdir(
                    parent,
                    parents=True
                )

        encoded = content.encode(encoding)

        import base64

        data = base64.b64encode(encoded).decode("ascii")

        command = (
            f"echo {shlex.quote(data)} | "
            f"base64 -d > {shlex.quote(remote_path)}"
        )

        self.execute(command)

    def append_file(
        self,
        remote_path: str,
        content: str,
        encoding: str = "utf-8"
    ):
        encoded = content.encode(encoding)

        import base64

        data = base64.b64encode(encoded).decode("ascii")

        command = (
            f"echo {shlex.quote(data)} | "
            f"base64 -d >> {shlex.quote(remote_path)}"
        )

        self.execute(command)

    def get_sha256(
        self,
        remote_path: str
    ) -> str:
        command = (
            f"sha256sum "
            f"{shlex.quote(remote_path)} | "
            f"awk '{{print $1}}'"
        )

        return self.execute(command).strip()

    def get_md5(
        self,
        remote_path: str
    ) -> str:
        command = (
            f"md5sum "
            f"{shlex.quote(remote_path)} | "
            f"awk '{{print $1}}'"
        )

        return self.execute(command).strip()

    def calculate_crc(self, path: str) -> int:
        script = (
            "import sys\n"
            "from zlib import crc32\n"
            "with open(sys.argv[1], 'rb') as f:\n"
            "    return_value = crc32(f.read()) & 0xFFFFFFFF\n"
            "print(return_value)"
        )

        command = (
            "python3 -c "
            + shlex.quote(script)
            + " "
            + shlex.quote(path)
        )

        result = self.execute(command)

        try:
            return int(result.strip())
        except ValueError as e:
            raise SSHFileError(
                f"无法解析 CRC32: {path}, result={result!r}"
            ) from e

    def scan_files_crc32(
        self,
        remote_dir: str,
        workers: Optional[int] = None
    ) -> List[Dict[str, int]]:
        script = (
            "import os\n"
            "from zlib import crc32\n"
            "from concurrent.futures import ThreadPoolExecutor\n"
            "\n"
            "root = os.path.abspath(" + repr(remote_dir) + ")\n"
            "\n"
            "def calculate(path):\n"
            "    try:\n"
            "        size = os.path.getsize(path)\n"
            "        with open(path, 'rb') as f:\n"
            "            crc = crc32(f.read()) & 0xFFFFFFFF\n"
            "        return path, size, crc\n"
            "    except (OSError, IOError):\n"
            "        return None\n"
            "\n"
            "files = []\n"
            "for current_root, dirs, names in os.walk(root):\n"
            "    for name in names:\n"
            "        path = os.path.join(current_root, name)\n"
            "        if os.path.isfile(path):\n"
            "            files.append(path)\n"
            "\n"
            f"workers = {workers or 'None'}\n"
            "with ThreadPoolExecutor(max_workers=workers) as executor:\n"
            "    for result in executor.map(calculate, files):\n"
            "        if result is not None:\n"
            "            path, size, crc = result\n"
            "            print(f'{path}\\t{size}\\t{crc}')\n"
        )

        command = (
            "python3 -c "
            + shlex.quote(script)
        )

        output = self.execute(
            command,
            timeout=3600
        )

        result = []

        for line in output.splitlines():
            parts = line.split("\t")

            if len(parts) != 3:
                continue

            result.append({
                "path": parts[0],
                "size": int(parts[1]),
                "crc32": int(parts[2])
            })

        return result

    def get_system_info(self) -> Dict[str, str]:
        command = """
hostname
uname -a
cat /etc/os-release | sed -n 's/^PRETTY_NAME=//p'
nproc
free -b | awk '/Mem:/ {print $2}'
free -b | awk '/Mem:/ {print $3}'
""".strip()

        result = self.execute(command).splitlines()

        while len(result) < 6:
            result.append("")

        return {
            "hostname": result[0],
            "kernel": result[1],
            "os": result[2].strip('"'),
            "cpu_count": result[3],
            "memory_total": result[4],
            "memory_used": result[5]
        }

    def ping(self) -> bool:
        try:
            return self.test_connection()
        except SSHServerError:
            return False

    def close(self):
        pass

    def __enter__(self):
        self.test_connection()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

