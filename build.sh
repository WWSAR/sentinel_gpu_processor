#!/bin/bash
# 1. 编译 CUDA/C++ 二进制
cmake -S csrc -B build -G Ninja \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_PREFIX_PATH=$PREFIX \
    -DCMAKE_INSTALL_PREFIX=$PREFIX

cmake --build build --target install

# 2. 安装 Python 包并把二进制打包进去
$PYTHON -m pip install . --no-deps -vv