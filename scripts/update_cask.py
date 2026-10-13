import argparse
import hashlib
import re
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("tag")
    parser.add_argument("--artifacts", type=Path, default=Path("release"))
    args = parser.parse_args()
    if not re.fullmatch(r"v\d+(?:\.\d+){1,2}", args.tag):
        parser.error("Expected a version tag such as v0.1")
    hashes = {
        arch: hashlib.sha256((args.artifacts / f"vkdump-macos-{arch}.zip").read_bytes()).hexdigest()
        for arch in ("arm64", "x86_64")
    }
    cask = f'''cask "vkdump" do
  arch arm: "arm64", intel: "x86_64"

  version "{args.tag[1:]}"
  sha256 arm:   "{hashes['arm64']}",
         intel: "{hashes['x86_64']}"

  url "https://github.com/akkez/vkdump/releases/download/v#{{version}}/vkdump-macos-#{{arch}}.zip"
  name "VKdump"
  desc "Parse and export VK chat archives"
  homepage "https://github.com/akkez/vkdump"

  depends_on macos: :sonoma

  app "vkdump-v#{{version}}/VKdump.app"

  postflight_steps do
    run "/usr/bin/xattr",
        args: ["-rd", "com.apple.quarantine", "{{{{appdir}}}}/VKdump.app"]
  end
end
'''
    path = Path("Casks/vkdump.rb")
    path.parent.mkdir(exist_ok=True)
    path.write_text(cask, encoding="utf-8")


if __name__ == "__main__":
    main()
