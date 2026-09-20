class Config:
    threads = 32
    max_threads = threads * 7
    proxy = None
    retries = 5

    tool_download_url = (
        "https://github.com/BlueArchive-Translation/"
        "BlueArchive-Tools-CLI/releases/latest/download/"
        "BlueArchiveTools.{platform_id}.zip"
    )
    env_file = "config/{server}.env"
    FlatData = "git@github.com:beichen23333/BA-FlatData.git"
    pc_src = "git@github.com:BlueArchive-Translation/BA-PC.git"
    apk_src = "https://github.com/BlueArchive-Translation/BA-APKSRC.git"
    api = "git@github.com:BlueArchive-Translation/BlueArchive-API.git"

    servers = {
        "JP": {
            "region": "JP",
            "platform": "Android",
            "data_path": "assets/bin/Data",
            "replace_path": "assets",
            "gt4_path": "assets/gt4.js",
            "sdk_config_path": "assets/SDKConfigSettings.json",
            "modify_login": True,
            "gateway_url": "https://prod-gateway.bluearchiveyostar.com:5100/api/gateway",
            "game_url": "https://prod-game.bluearchiveyostar.com:5000/api/gateway",
        },
        "JPiOS": {
            "region": "JP",
            "platform": "iOS",
            "data_path": "Payload/BlueArchive.app/Data",
            "replace_path": "Payload/BlueArchive.app/Data/Raw",
            "gt4_path": "Payload/BlueArchive.app/GTCaptcha4.bundle/gt4.js",
            "sdk_config_path": "Payload/BlueArchive.app/SDKConfigSettings.json",
            "modify_login": False,
            "gateway_url": "https://prod-gateway.bluearchiveyostar.com:5100/api/gateway",
            "game_url": "https://prod-game.bluearchiveyostar.com:5000/api/gateway",
        },
        "JPPC": {
            "region": "JP",
            "platform": "Windows",
            "data_path": "BlueArchive_Data",
            "replace_path": "BlueArchive_Data/StreamingAssets",
            "gt4_path": "",
            "sdk_config_path": "",
            "modify_login": False,
            "gateway_url": "https://prod-gateway.bluearchiveyostar.com:5100/api/gateway",
            "game_url": "https://prod-game.bluearchiveyostar.com:5000/api/gateway",
        },
        "GL": {
            "region": "GL",
            "platform": "Android",
            "data_path": "assets/bin/Data",
            "replace_path": "assets",
            "kr": {
                "region": "kr",
                "display_name": "Korea",
                "game_url": "https://nxm-kr-bagl.nexon.com:5000/api/",
                "gateway_url": "https://nxm-kr-bagl.nexon.com:5100/api/",
                "nxsid": "live-kr",
                "country": "KR",
                "locale": "ko-KR",
            },
            "tw": {
                "region": "tw",
                "display_name": "Taiwan/Hong Kong/Macau",
                "game_url": "https://nxm-tw-bagl.nexon.com:5000/api/",
                "gateway_url": "https://nxm-tw-bagl.nexon.com:5100/api/",
                "nxsid": "live-tw",
                "country": "TW",
                "locale": "zh-TW",
            },
            "asia": {
                "region": "asia",
                "display_name": "Asia",
                "game_url": "https://nxm-th-bagl.nexon.com:5000/api/",
                "gateway_url": "https://nxm-th-bagl.nexon.com:5100/api/",
                "nxsid": "live-asia",
                "country": "TH",
                "locale": "th-TH",
            },
            "na": {
                "region": "na",
                "display_name": "North America",
                "game_url": "https://nxm-or-bagl.nexon.com:5000/api/",
                "gateway_url": "https://nxm-or-bagl.nexon.com:5100/api/",
                "nxsid": "live-na",
                "country": "US",
                "locale": "en-US",
            },
            "global": {
                "region": "global",
                "display_name": "Global/Europe",
                "game_url": "https://nxm-eu-bagl.nexon.com:5000/api/",
                "gateway_url": "https://nxm-eu-bagl.nexon.com:5100/api/",
                "nxsid": "live-global",
                "country": "GB",
                "locale": "en-GB",
            },
        },
        "GLPC": {
            "region": "GL",
            "platform": "Windows",
            "data_path": "BlueArchive_Data",
            "replace_path": "BlueArchive_Data/StreamingAssets",
            "gt4_path": "",
            "sdk_config_path": "",
            "modify_login": False,
        },
        "GLiOS": {
            "region": "GL",
            "platform": "iOS",
            "data_path": "Payload/BlueArchive.app/Data",
            "replace_path": "Payload/BlueArchive.app/Data/Raw",
        },
        "CN": {
            "region": "CN",
            "platform": "Android",
            "data_path": "assets/bin/Data",
            "replace_path": "assets",
            "gt4_path": "",
            "sdk_config_path": "",
            "modify_login": False,
            "gateway_url": "",
            "game_url": "",
        },
    }

    YOSTAR_PUBLIC_KEY = """-----BEGIN PUBLIC KEY-----
MIICIjANBgkqhkiG9w0BAQEFAAOCAg8AMIICCgKCAgEAz1oODtVINPQsTIbFDiL2
IIj3YqDGxZ1WqJWfJrpo7ZqdnSIMXaVqVyM07Zguc9wczPrluDNowheP8NXe8uTR
prO7jpF9bOcrrit9Xyxf57NA5rIMqOj6/XgWzzptvvt3oUrOQJRTvEjusQCX79Ds
fhhbPRfdPzZUOPYa49MM385Z05ndNiTB9/yhxP5+5zm7TS3f6/o1Ntw9yXoRYmgR
8+JYGL6TpqtZHkqPTp6kIJrnsLyT1wRteh3oQKJaze0CM0DUUzFiqieRE8c8JAoV
jk/yRQUqVMoKbdpINYlWhF+GFE/ccSpqQymepHxfR0alspynqJwJgODCeHtwuno5
HEOwdbA8NanLVnE9qlvdeNkbODdt/XTHKOaXx+SUMHezUY0rEXp8WMim0i9IQTGa
YWeW25RjXIZswKdg0gLRJXYZXUgF3nVcqr0hi4kv1pgrFPsjI7yIThxLY0UfomkR
oe6sSBBtQUDI85s+1pZRcl8xygxOxMfFSk5RB3EntFmcf/AKaLYQbXN9RxgrvqRS
9koBBEnnrIN0zXDBioZUDOWVBeBZwAydpPGKu2mkqu3BI/Al92noEHc6fmkG5+Qm
b237xSh1DFbbQO6lxg4ABdsqgvZskYS+7BcQPjs1z2zftrnesFkqD7BhbqXUBuDY
D80B8Be5I483qSSy8DsW458CAwEAAQ==
-----END PUBLIC KEY-----"""

    NEXON_PUBLIC_KEY = """-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAtgS2BXLKIrI8OFIZi3ge
sVQLQq8Epwb0XLSKAmF15r5CT4EF9xaKXOIYho5Iwljdk3FDuhqCwnZL9Xrzb1o8
PPdi49woZgiFvf6hU5k9fH7NGCFq9aadhguGfLMtPo5yIp+awemawtSZkR6rZhWa
wH0DBh4grcSaqofZYhyT5ISOUm+BcTS1WYgujgcpuDyxE34EkUW4PNF8eqrefruw
7YzIPAI9k9yOQvu0yKobRD1pJHM47DN/WMDqRp8+mmImHUbL+tUoeHyEUU/HMk7N
WSmRTH2UXC/ijIW9yFTxzef3c1M+j2xG4O74ztAP88DKMZwsfgZzDNRe8bep2buS
OQIDAQAB
-----END PUBLIC KEY-----"""

    CN_PUBLOC_KEY = """-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAtgS2BXLKIrI8OFIZi3ge
sVQLQq8Epwb0XLSKAmF15r5CT4EF9xaKXOIYho5Iwljdk3FDuhqCwnZL9Xrzb1o8
PPdi49woZgiFvf6hU5k9fH7NGCFq9aadhguGfLMtPo5yIp+awemawtSZkR6rZhWa
wH0DBh4grcSaqofZYhyT5ISOUm+BcTS1WYgujgcpuDyxE34EkUW4PNF8eqrefruw
7YzIPAI9k9yOQvu0yKobRD1pJHM47DN/WMDqRp8+mmImHUbL+tUoeHyEUU/HMk7N
WSmRTH2UXC/ijIW9yFTxzef3c1M+j2xG4O74ztAP88DKMZwsfgZzDNRe8bep2buS
OQIDAQAB
-----END PUBLIC KEY-----"""

    @classmethod
    def get_region_config(cls, region):
        key = str(region).strip().lower()
        return cls.servers["GL"][key]
