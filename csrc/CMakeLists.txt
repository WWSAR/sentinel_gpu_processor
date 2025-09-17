cmake_minimum_required(VERSION 3.5)
project(sentinel_processor VERSION 0.1.0 LANGUAGES C CXX CUDA)
# Specify the C++ standard
set(CMAKE_CXX_STANDARD 11)
set(CMAKE_CXX_STANDARD_REQUIRED True)

# Automatically set architerctures based on specific flags
set(CMAKE_CUDA_ARCHITECTURES 60 70 75 80 89 90)

# set output folder
set(CMAKE_RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")

# Find TIFF package installed via vcpkg
find_package(TIFF REQUIRED)

if(UNIX)
    # Linux-specific config
    include_directories("$ENV{CONDA_PREFIX}/include")
    link_directories("$ENV{CONDA_PREFIX}/lib")
elseif(WIN32)
    # Windows-specific config
    # Find sqlite3 package installed via vcpkg
    find_package(unofficial-sqlite3 CONFIG REQUIRED)
    include(D:/vcpkg/scripts/buildsystems/vcpkg.cmake)
endif()

# Add source files
add_executable(readgeotiff src/readgeotiff.cpp)
add_executable(deramp_burst src/deramp_burst.cu src/sql_mod.cpp
               src/orbit.cu src/sario.cu)
add_executable(geo2rdr_reramp src/geo2rdr_reramp.cu src/sql_mod.cpp
               src/orbit.cu src/sario.cu src/bounds.cu)
add_executable(crossmul src/crossmul.cu src/sario.cu)

set_target_properties(geo2rdr_reramp PROPERTIES CUDA_SEPARABLE_COMPILATION ON)
# Add the include path for this specific target
target_include_directories(deramp_burst PRIVATE "include")
target_include_directories(geo2rdr_reramp PRIVATE "include")
target_include_directories(crossmul PRIVATE "include")

# Link TIFF to the project
target_link_libraries(readgeotiff PRIVATE TIFF::TIFF)
if(UNIX)
    # Linux-specific config
    target_link_libraries(deramp_burst PRIVATE sqlite3)
    target_link_libraries(geo2rdr_reramp PRIVATE sqlite3)
elseif(WIN32)
    # Windows-specific config
    target_link_libraries(deramp_burst PRIVATE unofficial::sqlite3::sqlite3)
    target_link_libraries(geo2rdr_reramp PRIVATE unofficial::sqlite3::sqlite3)
endif()

