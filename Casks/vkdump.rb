cask "vkdump" do
  arch arm: "arm64", intel: "x86_64"

  version "0.5"
  sha256 arm:   "25c889b900347cc747fa96ff78146d04e98833e2faf85bc17737c0c43bdca03e",
         intel: "1da6383a99ab4b3293d7c55ea24a6ef7a9c9fc33ca4cf054ee7f291eaf84b61b"

  url "https://github.com/akkez/vkdump/releases/download/v#{version}/vkdump-macos-#{arch}.zip"
  name "VKdump"
  desc "Parse and export VK chat archives"
  homepage "https://github.com/akkez/vkdump"

  depends_on macos: :sonoma

  app "vkdump-v#{version}/VKdump.app"

  postflight_steps do
    run "/usr/bin/xattr",
        args: ["-rd", "com.apple.quarantine", "{{appdir}}/VKdump.app"]
  end
end
