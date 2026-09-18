import argparse


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "server",
        choices=["JP", "JPPC", "JPiOS", "GL", "GLiOS", "CN"],
        help="选择服务器区域"
    )

    args = parser.parse_args()

    print(f"Server: {args.server}")


if __name__ == "__main__":
    main()
