import json
import requests
from typing import Any, Optional


class CF:
    class KV:
        def __init__(
            self,
            account_id: str,
            namespace_id: str,
            api_token: str
        ):
            self.base_url = (
                f"https://api.cloudflare.com/client/v4/"
                f"accounts/{account_id}/storage/kv/"
                f"namespaces/{namespace_id}"
            )

            self.headers = {
                "Authorization": f"Bearer {api_token}"
            }

        def put(
            self,
            key: str,
            value: Any,
            expiration: Optional[int] = None,
            expiration_ttl: Optional[int] = None
        ) -> bool:
            if isinstance(value, (dict, list)):
                value = json.dumps(
                    value,
                    ensure_ascii=False
                )

            params = {}

            if expiration is not None:
                params["expiration"] = expiration

            if expiration_ttl is not None:
                params["expiration_ttl"] = expiration_ttl

            response = requests.put(
                f"{self.base_url}/values/{key}",
                headers=self.headers,
                params=params,
                data=str(value).encode("utf-8")
            )

            response.raise_for_status()

            return response.json()["success"]

        def get(
            self,
            key: str,
            json_decode: bool = False
        ) -> Optional[Any]:
            response = requests.get(
                f"{self.base_url}/values/{key}",
                headers=self.headers
            )

            if response.status_code == 404:
                return None

            response.raise_for_status()

            value = response.text

            if json_decode:
                return json.loads(value)

            return value

        def delete(self, key: str) -> bool:
            response = requests.delete(
                f"{self.base_url}/values/{key}",
                headers=self.headers
            )

            response.raise_for_status()

            return response.json()["success"]

        def put_many(self, data: dict[str, Any]) -> bool:
            items = []

            for key, value in data.items():
                if isinstance(value, (dict, list)):
                    value = json.dumps(
                        value,
                        ensure_ascii=False
                    )

                items.append({
                    "key": key,
                    "value": str(value)
                })

            response = requests.put(
                f"{self.base_url}/bulk",
                headers={
                    **self.headers,
                    "Content-Type": "application/json"
                },
                json=items
            )

            response.raise_for_status()

            return response.json()["success"]

        def get_many(
            self,
            keys: list[str]
        ) -> dict[str, Any]:
            return {
                key: self.get(key)
                for key in keys
            }

    def __init__(
        self,
        account_id: str,
        api_token: str,
        kv_namespace_id: Optional[str] = None
    ):
        self.account_id = account_id
        self.api_token = api_token

        if kv_namespace_id:
            self.kv = self.KV(
                account_id=account_id,
                namespace_id=kv_namespace_id,
                api_token=api_token
            )
