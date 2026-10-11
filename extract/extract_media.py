import argparse
from pathlib import Path
from dotenv import load_dotenv
from utils.download import ResourceDownloader
from utils.config import Config

def jp_files(catalog):
    for v in catalog["Table"].values():
        v["Path"] = v["Path"].replace("\\", "/")
    return [Path(v["Path"]) for v in catalog["Table"].values()]

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("server", choices=["JP", "GL", "CN"], help="选择服务器区域")
    args = p.parse_args()

    download = ResourceDownloader(args.server)

    if args.server == "JP":
        catalog = download.get_media_catalog()
        files = jp_files(catalog)
        download.get_media_files(files, save_path="download")

