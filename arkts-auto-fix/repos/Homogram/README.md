<p align="center">
  <img src="./assets/logo.png" width="20%" style="border-radius:25%"/>
</p>

<h1 align="center">Homogram</h1>

Homogram is a 3rd-party Telegram client for [HarmonyOS 5](https://developer.huawei.com/consumer/cn/), driven by
ArkTS/ArkUI (UI-layer) and Rust (native-layer).

_This project is a hobby project and is not affiliated with either Telegram or Huawei._

**NEWS: Starting from 2nd Dec, 2024, [出境易](https://appgallery.huawei.com/app/detail?id=com.easy.hmos.abroad) provides a full-featured environment to download and use Android Apps. You can use the official Telegram client inside it's Android container.**

## Branches

- The `main` branch contains the latest most stable version of Homogram, though it may not be the most feature-rich, it
  will at least compile and run. (with as few bugs as possible)
- The `dev` branch may contain bugs, incomplete features, and other issues. It is not guaranteed to compile or run.

## Roadmap

- [x] Login with phone number, authorizing with verification code and (optionally) password
- [x] Loading chats and recent N messages
- [x] Rendering text and photo preview in messages
- [x] Sending text messages
- [x] Adaptive dark/light mode
- [x] Jump to reply in chat
- [ ] Message prefetching on scroll
- [ ] Full-screen view for medias
- [ ] Sending medias/files
- [ ] Reply to messages
- [ ] VOIP integration
- [ ] [HMS Push Kit](https://developer.huawei.com/consumer/cn/sdk/push-kit) (depends on Telegram's server-side
  support https://github.com/tdlib/td/issues/3057)
- [ ] Tons of features from official Telegram client...
- [ ] ...and even more!

A detailed Roadmap can be found in the [Homogram Project](https://github.com/orgs/HomoArk/projects/2) page.

## Building

### Prerequisites

- A Windows or MacOS device which supports [DecEco Studio](https://developer.huawei.com/consumer/cn/deveco-studio/) and
  Rust toolchain.
- Tested on Mate 60 Pro ALN-AL80 5.0.0.107, OpenHarmony SDK 5.0.1.106 (API 13 Beta 3) and Rust 1.82.0. Other
  configurations may work
  but are not guaranteed.

1. Clone the repository:
   ```shell
   git clone https://github.com/HomoArk/Homogram.git --recursive
   ```
2. [Obtain your own api_id](https://core.telegram.org/api/obtaining_api_id) for your application.
3. Fill out values into `features/home/src/main/rust/src/tg/config.rs` (there is a template file
   `config.rs.template`).
4. Setup Rust toolchain and ohos-rs for OpenHarmony: https://ohos.rs/docs/basic/quick-start.html
5. Build the native library in `features/home/src/main/native/homogrape`:
   ```shell
   cargo xtask dist ../../../../libs/arm64-v8a/
   ```
6. Correctly [configure](https://developer.huawei.com/consumer/cn/doc/harmonyos-guides-V5/ide-signing-V5) the signature
   information, and build the project in DevEco Studio.
7. Enjoy! (Or not, because it's still very feature-incomplete and suboptimal)

PRs are welcome! Or you definitely want to fork this project and make your own version. Just don't forget to let me test
if you implement something cool!

## Credits

- [grammers](https://github.com/Lonami/grammers), a set of Rust libraries to interact with Telegram's API.
- [ohos-rs](https://github.com/ohos-rs/ohos-rs), a`napi-rs` adaptation for OpenHarmony SDK.
- [一多开发实例-即时通讯](https://gitee.com/harmonyos_codelabs/MultiDeviceCommunication), a sample project for developing IM APPs on HarmonyOS Next.

These projects are the backbone of Homogram and without them, this project would not be possible. Tremendous
thanks to all contributors!

## License

This project is licensed under the Apache License, Version 2.0.
