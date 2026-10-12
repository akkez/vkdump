cask "vkdump" do
  arch arm: "arm64", intel: "x86_64"

  version "0.2"
  sha256 arm:   "32109200e98ac0110ce87729270e1fa29d331c7e5a27c2681941d61caf2b8d0a",
         intel: "d242ef3201701cb236b5125c6eb0cf2c69aa6b28313ce01c848e4615c8a6b9e7"

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
