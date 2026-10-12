cask "vkdump" do
  arch arm: "arm64", intel: "x86_64"

  version "0.3"
  sha256 arm:   "19d947ef5780ad58484bcd0a9b4db3a273089feb891f5ce499d7262a6ade704c",
         intel: "528c2926c47b937825ea284e55487ceedfe78ebf26bffc1adc09a1ceeec3b03a"

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
