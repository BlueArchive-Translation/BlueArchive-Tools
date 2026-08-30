from utils.sqlcipher import sqlite3
from utils.structure import DBColumn, DBTable


class TableDatabase:
    def __init__(self, database: str, password: str = None) -> None:
        self.database = database
        self.password = password

        self.connection = sqlite3.connect(self.database)

        cursor = self.connection.cursor()

        if password:
            # 日服文件被加密了，需要使用密钥解密，这里有密钥就用，没有则正常。
            cursor.execute(f"PRAGMA key = \"x'{password}'\";")

        try:
            cursor.execute("SELECT count(*) FROM sqlite_master;")
            print("数据库连接成功！")
        except Exception as e:
            print(f"连接失败，可能是密钥错误或格式不对: {e}")
            raise

    def __enter__(self) -> "TableDatabase":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.connection.close()

    def execute(self, sql: str) -> None:
        cursor = self.connection.cursor()
        cursor.execute(sql)
        self.connection.commit()

    def get_table_list(self) -> list[str]:
        """Get all table name in database as list.

        Returns:
            list[tuple]: Tables
        """
        cursor = self.connection.cursor()

        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table';"
        )

        return [
            table[0]
            for table in cursor.fetchall()
            if table
        ]

    def get_table_column_structure(
        self,
        table: str,
    ) -> list[DBColumn]:
        """Get data structure of table.

        Args:
            table (str): table_name

        Returns:
            list[Column]: A list store all columns structure.
        """
        cursor = self.connection.cursor()

        cursor.execute(
            f"PRAGMA table_info({table});"
        )

        return [
            DBColumn(
                name=col[1],
                data_type=col[2],
            )
            for col in cursor.fetchall()
        ]

    def get_table_data(
        self,
        table: str,
    ) -> tuple[list, list]:
        """Get all rows and table structure in table.

        Args:
            table (str): table_name

        Returns:
            tuple[list, list]: First list store the column_names.
            Second list store the rows.
        """
        cursor = self.connection.cursor()

        cursor.execute(
            f"SELECT * FROM {table};"
        )

        column_names = [
            description[0]
            for description in cursor.description
        ]

        rows = cursor.fetchall()

        return column_names, rows

    def update_table_data(
        self,
        table: str,
        column: list[str],
        data: list[list],
    ) -> None:
        cursor = self.connection.cursor()

        try:
            cursor.execute(
                "PRAGMA synchronous = OFF;"
            )

            cursor.execute(
                "PRAGMA journal_mode = MEMORY;"
            )

            cursor.execute(
                "BEGIN TRANSACTION;"
            )

            print(
                f"正在清空旧表 {table}..."
            )

            cursor.execute(
                f"DELETE FROM {table};"
            )

            placeholders = ", ".join(
                ["?"] * len(column)
            )

            sql = (
                f"INSERT INTO {table} "
                f"({', '.join(column)}) "
                f"VALUES ({placeholders});"
            )

            print(
                f"正在批量插入 "
                f"{len(data)} 行数据..."
            )

            cursor.executemany(
                sql,
                data,
            )

            self.connection.commit()

            print(
                f"表 {table} 更新成功！"
            )

        except Exception as e:

            self.connection.rollback()

            print(
                f"数据库写入失败，"
                f"已回滚: {e}"
            )

            raise e

    def set_password(
        self,
        password: str,
    ) -> None:
        """
        给数据库设置加密密钥。

        如果当前数据库本身就是 SQLCipher：
            后续 close() 时重新使用该密钥加密写回。

        如果当前数据库是普通 SQLite：
            后续 close() 时转换为 SQLCipher 数据库。
        """

        self.password = password

        cursor = self.connection.cursor()

        cursor.execute(
            f"PRAGMA key = \"x'{password}'\";"
        )


    def encrypt(self) -> None:
        """
        立即将当前数据库加密并写回原文件。

        不改变原来的数据库操作接口。
        """

        self.connection.commit()

        if hasattr(
            self.connection,
            "encrypt_database",
        ):
            self.connection.encrypt_database()

        else:
            raise RuntimeError(
                "当前 sqlite3 兼容层不支持 "
                "encrypt_database()"
            )

    @staticmethod
    def convert_to_list_dict(
        table: DBTable,
    ) -> list[dict]:
        """Convert table to list of json structure dict.

        Args:
            table (DBTable): Table to convert.

        Returns:
            list[dict]: A list include all the rows to key value pair.
        """
        table_rows = []

        for row in table.data:
            row_data = {}

            for col, value in zip(
                table.columns,
                row,
            ):
                row_data[col.name] = value

            if row_data:
                table_rows.append(
                    row_data
                )

        table_rows = [
            struct["Bytes"]
            for struct in table_rows
        ]

        return table_rows
