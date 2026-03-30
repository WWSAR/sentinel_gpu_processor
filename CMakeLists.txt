cmake_minimum_required(VERSION 3.5)
project(s1proc VERSION 0.1.0 LANGUAGES C CXX CUDA)
# Specify the C++ standard
set(CMAKE_CXX_STANDARD 11)
set(CMAKE_CXX_STANDARD_REQUIRED True)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

add_subdirectory(s1proc/csrc)

