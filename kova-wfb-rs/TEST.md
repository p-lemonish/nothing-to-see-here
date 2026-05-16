# Testing with `uv`

Use these commands to run Python tests with `uv` using **dynamic library loading only** (no static `.a` linking and no hardcoded absolute paths).

## 1. Install `uv` (if needed)

```bash
brew install uv
```

## 2. Run unit tests (fast path)

From `kova-wfb-rs/python`:

```bash
uv sync --extra test
uv run pytest -q
```

## 3. Run tests with runtime FFI smoke enabled (dynamic lib)

From `kova-wfb-rs`:

```bash
cargo build --release
cd python
```

Set the dynamic library path without hardcoding an absolute path:

```bash
if [ "$(uname)" = "Darwin" ]; then
  LIB_EXT="dylib"
else
  LIB_EXT="so"
fi
export WFB_RS_LIB_PATH="$(cd .. && pwd)/target/release/libwfb_rs.${LIB_EXT}"
uv run pytest -q
```

Notes:
- This uses the shared library (`.dylib`/`.so`) only.
- If the dynamic library is missing, the runtime ABI smoke test is skipped automatically.
