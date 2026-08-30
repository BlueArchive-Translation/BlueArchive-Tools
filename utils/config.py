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
    pc_src = "git@github.com:beichen23333/BA-PC-SRC.git"
    apk_src = "https://github.com/BlueArchive-Translation/BA-APKSRC.git"
