# Third-party directories

This repository expects two bundled dependency areas at the top level:

- `ixwebsocket/` contains the vendored IXWebSocket source tree used by the native CMake targets.
- `libs/arm64-v8a/` contains the prebuilt static archives that CMake imports: `libixwebsocket.a`, `libmbedtls.a`, `libmbedx509.a`, and `libmbedcrypto.a`.

## How to restore `ixwebsocket/`

If the directory is missing, fetch it from the upstream IXWebSocket project and copy the `ixwebsocket/` source folder into this repo root:

```bash
git clone --depth 1 https://github.com/machinezone/IXWebSocket.git /tmp/IXWebSocket
cp -R /tmp/IXWebSocket/ixwebsocket ./ixwebsocket
```

## How to restore `libs/arm64-v8a/`

The `libs/arm64-v8a/` directory is not downloaded from a package manager here. It is usually restored from a prior build or copied from another checkout that already has the Android arm64-v8a static libraries built.

To recreate it manually:

```bash
mkdir -p libs/arm64-v8a
# Copy the four static archives into libs/arm64-v8a/
```

After that, ensure these files exist in `libs/arm64-v8a/`:

- `libixwebsocket.a`
- `libmbedtls.a`
- `libmbedx509.a`
- `libmbedcrypto.a`

The CMake build looks for those exact paths.