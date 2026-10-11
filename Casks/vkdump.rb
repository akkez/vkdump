cask "vkdump" do
  arch arm: "arm64", intel: "x86_64"

  version "0.1"
  sha256 arm:   "79f4da865db85a74d28d227f763de5d6a2469eabb80dce897aaa595a249b94b9",
         intel: "e28011c4f491190badda7c302f9b93a8f402906b7102cab22783b4005d82e17d"

  url "https://github.com/akkez/vkdump/releases/download/v#{version}/vkdump-macos-#{arch}.zip"
  name "VKdump"
  desc "Parse and export VK chat archives"
  homepage "https://github.com/akkez/vkdump"

  depends_on macos: :sonoma

  app "VKdump.app"

  postflight_steps do
    run "/usr/bin/xattr",
        args: ["-rd", "com.apple.quarantine", "{{appdir}}/VKdump.app"]
  end
end
