cmake_minimum_required(VERSION 3.5)
project(s1proc VERSION 0.1.0 LANGUAGES C CXX CUDA)
set(CMAKE_CXX_STANDARD 11)
set(CMAKE_CXX_STANDARD_REQUIRED True)

# Automatically set architectures based on specific flags
set(CMAKE_CUDA_ARCHITECTURES 60 70 75 80 89 90)

# Set output folder
set(CMAKE_RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")

# Cross-platform dependency management
if(WIN32)
    # Windows: Use vcpkg
    if(DEFINED ENV{VCPKG_ROOT})
        set(CMAKE_TOOLCHAIN_FILE "$ENV{VCPKG_ROOT}/scripts/buildsystems/vcpkg.cmake")
    else()
        message(FATAL_ERROR "VCPKG_ROOT environment variable not set. Please set it to your vcpkg installation directory.")
    endif()
elseif(UNIX AND NOT APPLE)
    # Linux: Try conda first, then system
    if(DEFINED ENV{CONDA_PREFIX})
        include_directories("$ENV{CONDA_PREFIX}/include")
        link_directories("$ENV{CONDA_PREFIX}/lib")
    endif()
elseif(APPLE)
    # macOS: Homebrew support
    if(DEFINED ENV{HOMEBREW_PREFIX})
        include_directories("$ENV{HOMEBREW_PREFIX}/include")
        link_directories("$ENV{HOMEBREW_PREFIX}/lib")
    endif()
endif()

# Find packages (order matters: tries custom paths first, then system)
find_package(TIFF REQUIRED)

if(WIN32)
    find_package(unofficial-sqlite3 CONFIG REQUIRED)
else()
    find_package(SQLite3 REQUIRED)
endif()

# Add source files
add_executable(readgeotiff src/readgeotiff.cpp)
add_executable(deramp_burst src/deramp_burst.cu src/sql_mod.cpp
               src/orbit.cu src/sario.cu)
add_executable(geo2rdr_reramp src/geo2rdr_reramp.cu src/sql_mod.cpp
               src/orbit.cu src/sario.cu src/bounds.cu)
add_executable(crossmul src/crossmul.cu src/sario.cu)
add_executable(crossmul_sec src/crossmul_sec.cu src/sario.cu)

set_target_properties(geo2rdr_reramp PROPERTIES CUDA_SEPARABLE_COMPILATION ON)

# Add include directories for all targets
target_include_directories(readgeotiff PRIVATE "include")
target_include_directories(deramp_burst PRIVATE "include")
target_include_directories(geo2rdr_reramp PRIVATE "include")
target_include_directories(crossmul PRIVATE "include")
target_include_directories(crossmul_sec PRIVATE "include")

# Link libraries with platform-specific handling
target_link_libraries(readgeotiff PRIVATE TIFF::TIFF)

if(WIN32)
    target_link_libraries(deramp_burst PRIVATE TIFF::TIFF unofficial::sqlite3::sqlite3)
    target_link_libraries(geo2rdr_reramp PRIVATE TIFF::TIFF unofficial::sqlite3::sqlite3)
else()
    target_link_libraries(deramp_burst PRIVATE TIFF::TIFF SQLite::SQLite3)
    target_link_libraries(geo2rdr_reramp PRIVATE TIFF::TIFF SQLite::SQLite3)
endif()

# Install CUDA executables into the Python package: s1proc/bin/
install(TARGETS readgeotiff deramp_burst geo2rdr_reramp crossmul crossmul_sec
        RUNTIME DESTINATION s1proc/bin
)
