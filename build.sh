#!/bin/bash
set -e
export LANG=C
export LC_ALL=C
export NVCC_APPEND_FLAGS="${NVCC_APPEND_FLAGS} -std=c++14"

cmake -S csrc -B build -G Ninja \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX="$PREFIX/s1proc" \
    -DCMAKE_PREFIX_PATH="$PREFIX"
    
cmake --build build --target install -- -j$(nproc)

$PYTHON -m pip install . --no-deps --ignore-installed --no-cache-dir -vv
