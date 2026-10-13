cask "vkdump" do
  arch arm: "arm64", intel: "x86_64"

  version "0.4"
  sha256 arm:   "beb27abc19004c30f3932511c30f0976a634f0d9455efcd9eb256fe5df4cd0b0",
         intel: "74df0d42c44e708f68c250fc514d67da8f6366cfac20e3347714b8150fd06a4e"

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
