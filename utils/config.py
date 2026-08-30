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
