# CMakeLists.txt

cmake_minimum_required(VERSION 3.10)

project(MyProject)

# Platform-aware dependency management
if (WIN32)
    # Windows-specific dependencies
    message(STATUS "Configuring for Windows")
    find_package(SomeWindowsDependency REQUIRED)
elseif (APPLE)
    # macOS-specific dependencies
    message(STATUS "Configuring for macOS")
    find_package(SomeMacDependency REQUIRED)
elseif (UNIX)
    # Linux-specific dependencies
    message(STATUS "Configuring for Linux")
    find_package(SomeLinuxDependency REQUIRED)
else()
    message(FATAL_ERROR "Unsupported platform!")
endif()

# Add your project files and libraries here
add_executable(MyExecutable main.cpp)