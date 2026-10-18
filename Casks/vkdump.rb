cask "vkdump" do
  arch arm: "arm64", intel: "x86_64"

  version "0.6"
  sha256 arm:   "f842cacd645ba07acff087dc71b47625bb249fda5e46bdecbfa550205246b5f5",
         intel: "dcae9ec22ed482a2df2e17f7b5fa5dfa1040cc554ee840afb8f796fb4db4e933"

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
