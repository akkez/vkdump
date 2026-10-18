# Android preparation

The Start wizard downloads verified Android files into a private temporary
directory. It does not unpack or run Android, install VK, extract tokens, accept
SDK licenses, or modify the user's Android SDK, AVDs, or adb server.

| Host | Emulator | System image | Future VM acceleration |
| --- | --- | --- | --- |
| macOS Apple Silicon | darwin_aarch64 | Android 14 / arm64-v8a | HVF |
| macOS Intel | darwin_x64 | Android 14 / x86_64 | HVF |
| Linux x64 | linux_x64 | Android 14 / x86_64 | KVM |
| Windows x64 | windows_x64 | Android 14 / x86_64 | WHPX |

Linux ARM and Windows ARM are unsupported. All four download catalogs ship in
every build; only the current host's files are fetched.

`artifacts.json` contains the URLs, sizes and checksums. Google catalog entries
must match before a new download starts. Verified cached files work offline.
Downloads use `.part` files, validated Range responses, three attempts with
backoff, checksum verification and atomic rename. Pause/exit preserves partials;
an exclusive OS file lock prevents two instances writing the same download.

The temporary directory is `tempfile.gettempdir()/vkdump-android-<uid>/<target>`
(`vkdump-android-user` on Windows, under the user's temporary directory).
The operating system can remove these files. `preparation.json` records verified
downloads and the proposed GPU modes: `auto`, then `software`; Linux without
usable KVM starts with `software`. Acceleration/GPU checks remain pending until
the emulator launch stage is implemented. Software graphics does not replace a
VM hypervisor. Native APK ABI compatibility must also be checked at installation;
the pinned APK is not assumed to include x86 libraries.
GPU modes follow [Google's emulator documentation](https://developer.android.com/studio/run/emulator-acceleration).

## APK sources

Only the original VK 8.199 / 59196 APK with SHA-256
`9eec7239a71bde0f7d748a55621d5619fd2cb3a2dd2fda80ad4dcf745727000a`
is accepted. The verified original is stored in `dist/` in the GitHub repository
using Git LFS. It is excluded from application bundles, wheels and source archives.
No other version is silently substituted, even with the same version name.

An existing verified cached file or an explicitly selected local APK takes
precedence. Automatic sources are tried in `apk.sources` order: the pinned GitHub
asset, APKPure, APKMirror. The GitHub URL uses `media.githubusercontent.com` and
the asset commit ID, so it serves the LFS payload rather than its pointer.
Mirror links are resolved each time rather than saving signed URLs.
Unavailable builds, source failures and wrong checksums advance to the next
source. If all fail, the wizard offers a file picker. Source results are saved
in `apk-sources-result.json` in the download directory.

Additional sources can be bundled into `apk.sources`, or appended locally using
`apk-sources.json` in the download directory. Supported entries:

```json
[
  {
    "name": "Personal GitHub release",
    "type": "github-release",
    "api_url": "https://api.github.com/repos/OWNER/REPOSITORY/releases/tags/android-assets"
  },
  {
    "name": "Personal GitHub storage",
    "type": "direct",
    "url": "https://raw.githubusercontent.com/OWNER/REPOSITORY/COMMIT/com.vkontakte.android-8.199-59196.apk"
  }
]
```

Replace the example owner/repository/ref before using these entries. GitHub
Releases must contain an asset named `com.vkontakte.android-8.199-59196.apk`. The same pinned
checksum applies to all sources, including GitHub. Private repositories requiring
authentication are not supported. Nothing is uploaded or published by VKdump.
