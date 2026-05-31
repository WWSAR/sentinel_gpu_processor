#!/bin/bash

echo "=== [Linux] Starting GPU-Accelerated Build ==="
echo "Using GCC: $(gcc --version | head -n 1)"
echo "Using NVCC: $(nvcc --version | head -n 1)"

# 2. CMake 编译：Linux 下通常使用默认的 Unix Makefiles 或 Ninja
# 这里的 $PREFIX 是 Conda 沙盒自动为你提供的环境变量（等同于 Windows 里的 %SRC_DIR%\s1proc）
cmake -S csrc -B build -G Ninja \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX="$PREFIX/s1proc"
    
cmake --build build --target install -- -j$(nproc)

# 3. 将编译好的二进制扩展与 Python 源码一同封包装箱
$PYTHON -m pip install . --no-deps --ignore-installed --no-cache-dir -vv