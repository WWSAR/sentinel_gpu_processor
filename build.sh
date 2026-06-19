#!/bin/bash

echo "=== [Linux] Check compilers ==="
echo "Using GCC: $(gcc --version | head -n 1)"
echo "Using NVCC: $(nvcc --version | head -n 1)"

cmake -S csrc -B build -G Ninja \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX="$PREFIX/s1proc" \
    -DCMAKE_PREFIX_PATH="$PREFIX;$PREFIX/Library"
    
cmake --build build --target install -- -j$(nproc)

$PYTHON -m pip install . --no-deps --ignore-installed --no-cache-dir -vv
