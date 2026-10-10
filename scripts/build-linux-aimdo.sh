#!/bin/bash

set -euo pipefail

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
BUILD_DIR="$ROOT_DIR/build"
CUDA_OUTPUT_PATH="$ROOT_DIR/comfy_aimdo/aimdo.so"
ROCM_OUTPUT_PATH="$ROOT_DIR/comfy_aimdo/aimdo_rocm.so"
XPU_OUTPUT_PATH="$ROOT_DIR/comfy_aimdo/aimdo_xpu.so"
FUNCHOOK_VERSION=1.1.3
FUNCHOOK_SRC="$BUILD_DIR/funchook-$FUNCHOOK_VERSION"
FUNCHOOK_TARBALL="$BUILD_DIR/funchook-$FUNCHOOK_VERSION.tar.gz"

ARCH=$(uname -m)

# Linkless builds no longer need CUDA stubs; only the funchook backend varies.
if [ "$ARCH" = "aarch64" ] || [ "$ARCH" = "arm64" ]; then
    FUNCHOOK_DISASM=capstone
    FUNCHOOK_BUILD_DIR="$BUILD_DIR/funchook-$FUNCHOOK_VERSION-capstone"
else
    FUNCHOOK_DISASM=distorm
    FUNCHOOK_BUILD_DIR="$BUILD_DIR/funchook-$FUNCHOOK_VERSION-distorm"
fi

if [ ! -f "$FUNCHOOK_SRC/CMakeLists.txt" ]; then
    URL="https://github.com/kubo/funchook/releases/download/v$FUNCHOOK_VERSION/funchook-$FUNCHOOK_VERSION.tar.gz"

    mkdir -p "$BUILD_DIR"
    curl -fL "$URL" -o "$FUNCHOOK_TARBALL"
    rm -rf "$FUNCHOOK_SRC"
    tar -xzf "$FUNCHOOK_TARBALL" -C "$BUILD_DIR"
fi

if [ "$FUNCHOOK_DISASM" = "capstone" ] && ! grep -q "CAPSTONE_CMAKELISTS" "$FUNCHOOK_SRC/CMakeLists.txt"; then
    patch -d "$FUNCHOOK_SRC" -p1 --forward <<'PATCH'
--- a/CMakeLists.txt
+++ b/CMakeLists.txt
@@ -86,6 +86,11 @@ if (DISASM_CAPSTONE)
   execute_process(COMMAND "${CMAKE_COMMAND}" --build .
       WORKING_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}/capstone-download"
   )
 
+  # Capstone 4.0.2 still sets CMP0048 to OLD, which newer CMake rejects.
+  file(READ "${CMAKE_CURRENT_BINARY_DIR}/capstone-src/CMakeLists.txt" CAPSTONE_CMAKELISTS)
+  string(REPLACE "cmake_policy (SET CMP0048 OLD)" "cmake_policy (SET CMP0048 NEW)" CAPSTONE_CMAKELISTS "${CAPSTONE_CMAKELISTS}")
+  file(WRITE "${CMAKE_CURRENT_BINARY_DIR}/capstone-src/CMakeLists.txt" "${CAPSTONE_CMAKELISTS}")
+
   string(TOUPPER ${FUNCHOOK_CPU} FUNCHOOK_CPU_UPPER)
PATCH
fi

FUNCHOOK_READY=false
if [ "$FUNCHOOK_DISASM" = "capstone" ]; then
    [ -f "$FUNCHOOK_BUILD_DIR/libfunchook.a" ] && \
    [ -f "$FUNCHOOK_BUILD_DIR/capstone-build/libcapstone.a" ] && \
    FUNCHOOK_READY=true
else
    [ -f "$FUNCHOOK_BUILD_DIR/libfunchook.a" ] && \
    [ -f "$FUNCHOOK_BUILD_DIR/libdistorm.a" ] && \
    FUNCHOOK_READY=true
fi

if [ "$FUNCHOOK_READY" = "false" ]; then
    if ! command -v cmake >/dev/null 2>&1; then
        echo "cmake is required to build funchook" >&2
        exit 1
    fi

    mkdir -p "$FUNCHOOK_BUILD_DIR"

    cmake -S "$FUNCHOOK_SRC" -B "$FUNCHOOK_BUILD_DIR" \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_POLICY_VERSION_MINIMUM=3.5 \
        -DCMAKE_POSITION_INDEPENDENT_CODE=ON \
        -DFUNCHOOK_BUILD_SHARED=OFF \
        -DFUNCHOOK_BUILD_STATIC=ON \
        -DFUNCHOOK_BUILD_TESTS=OFF \
        -DFUNCHOOK_DISASM="$FUNCHOOK_DISASM" \
        -DFUNCHOOK_INSTALL=OFF

    cmake --build "$FUNCHOOK_BUILD_DIR" -j"$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 1)"
fi

mkdir -p "$(dirname -- "$CUDA_OUTPUT_PATH")"

# Collect funchook + disassembler static libraries
FUNCHOOK_LIBS="$FUNCHOOK_BUILD_DIR/libfunchook.a"
if [ "$FUNCHOOK_DISASM" = "capstone" ]; then
    FUNCHOOK_LIBS="$FUNCHOOK_LIBS $FUNCHOOK_BUILD_DIR/capstone-build/libcapstone.a"
else
    FUNCHOOK_LIBS="$FUNCHOOK_LIBS $FUNCHOOK_BUILD_DIR/libdistorm.a"
fi

# shellcheck disable=SC2086
gcc -shared -o "$CUDA_OUTPUT_PATH" -fPIC -O2 -g -pthread \
    -DAIMDO_CUDA \
    ${AIMDO_EXTRA_CFLAGS:-} \
    "$ROOT_DIR"/src/*.c "$ROOT_DIR"/src-cuda/dispatch.c "$ROOT_DIR"/src-posix/*.c \
    -I"$ROOT_DIR/src" -I"$FUNCHOOK_SRC/include" \
    $FUNCHOOK_LIBS \
    -ldl

# shellcheck disable=SC2086
gcc -shared -o "$ROCM_OUTPUT_PATH" -fPIC -O2 -g -pthread \
    -D__HIP_PLATFORM_AMD__ \
    ${AIMDO_EXTRA_CFLAGS:-} \
    "$ROOT_DIR"/src/*.c "$ROOT_DIR"/src-hip/dispatch.c "$ROOT_DIR"/src-posix/*.c \
    -I"$ROOT_DIR/src" -I"$FUNCHOOK_SRC/include" \
    $FUNCHOOK_LIBS \
    -ldl

# XPU（Intel Arc / oneAPI Level Zero）后端：不链接 funchook，且排除 CUDA 专属的
# cuda-funchooks.c（XPU 通过 PyTorch XPUPluggableAllocator 注册 alloc_fn/free_fn）。
XPU_POSIX_SOURCES=$(ls "$ROOT_DIR"/src-posix/*.c | grep -v '/cuda-funchooks\.c$')

# shellcheck disable=SC2086
gcc -shared -o "$XPU_OUTPUT_PATH" -fPIC -O2 -g -pthread \
    -DAIMDO_XPU \
    ${AIMDO_EXTRA_CFLAGS:-} \
    "$ROOT_DIR"/src/*.c "$ROOT_DIR"/src-xpu/dispatch.c $XPU_POSIX_SOURCES \
    -I"$ROOT_DIR/src" \
    -ldl
