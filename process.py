import argparse
from extract.extract_table import TablePublisher
from extract.extract_bundle import BundlePublisher

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("server", choices=["JP", "GL", "CN"], help="选择服务器区域")
    parser.add_argument("type", choices=["Table", "Media", "Bundle"], help="选择资源类型")
    parser.add_argument("process", choices=["Extract", "Build"], help="选择处理类型")
    args = parser.parse_args()

    if args.process == "Extract":
        if args.type == "Table":
            TablePublisher(args.server).run()
        elif args.type == "Bundle":
            BundlePublisher(args.server).run()
        elif args.type == "Media":
            pass  # Media Extract

    elif args.process == "Repack":
        pass  # Repack
