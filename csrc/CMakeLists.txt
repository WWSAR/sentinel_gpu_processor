cmake_minimum_required(VERSION 3.21)

if(UNIX)
    add_subdirectory(snaphu)
endif()

project(s1proc VERSION 0.1.0 LANGUAGES C CXX CUDA)

set(CMAKE_CXX_STANDARD 11)
set(CMAKE_CXX_STANDARD_REQUIRED True)
set(CMAKE_CXX_FLAGS "${CMAKE_CXX_FLAGS} -D_GLIBCXX_USE_CXX11_ABI=1")

# Automatically set architectures based on specific flags
set(CMAKE_CUDA_ARCHITECTURES 75 80 89 90)

# Find packages (order matters: tries custom paths first, then system)
find_package(TIFF REQUIRED)
find_package(SQLite3 REQUIRED)
#find_package(CUDAToolkit REQUIRED)
find_package(CUDAToolkit REQUIRED COMPONENTS cufft)

if(DEFINED ENV{BUILD_PREFIX})
    if(WIN32)
        list(APPEND CUDAToolkit_INCLUDE_DIRS "$ENV{BUILD_PREFIX}/Library/include")
    else()
        list(APPEND CUDAToolkit_INCLUDE_DIRS "$ENV{BUILD_PREFIX}/targets/x86_64-linux/include")
        list(APPEND CUDAToolkit_INCLUDE_DIRS "$ENV{BUILD_PREFIX}/include")
    endif()
endif()

if(DEFINED ENV{BUILD_PREFIX})
    if(WIN32)
        list(APPEND CUDAToolkit_INCLUDE_DIRS "$ENV{BUILD_PREFIX}/Library/include")
    else()
        list(APPEND CUDAToolkit_INCLUDE_DIRS "$ENV{BUILD_PREFIX}/targets/x86_64-linux/include")
        list(APPEND CUDAToolkit_INCLUDE_DIRS "$ENV{BUILD_PREFIX}/include")
    endif()
endif()

message(STATUS "-------------------------------------------------")
message(STATUS "[s1proc DEBUG] CUDA Include Dirs: ${CUDAToolkit_INCLUDE_DIRS}")
message(STATUS "-------------------------------------------------")

#set(CMAKE_INCLUDE_SYSTEM_FLAG_CUDA "-I")

if(NOT TARGET SQLite::SQLite3 AND TARGET SQLite3::SQLite3)
    add_library(SQLite::SQLite3 ALIAS SQLite3::SQLite3)
endif()

if(NOT TARGET SQLite3::SQLite3 AND TARGET SQLite::SQLite3)
    add_library(SQLite3::SQLite3 ALIAS SQLite::SQLite3)
endif()

# Add source files

add_executable(readgeotiff src/readgeotiff.cpp)
add_executable(deramp_burst src/deramp_burst.cu src/sql_mod.cpp
               src/orbit.cu src/sario.cu)
add_executable(geo2rdr_reramp src/geo2rdr_reramp.cu src/sql_mod.cpp
               src/orbit.cu src/sario.cu src/bounds.cu)
add_executable(crossmul src/crossmul.cu src/sario.cu)
add_executable(phase_similarity src/phase_similarity.cu)
add_executable(goldstein src/goldstein.cu)

set_target_properties(geo2rdr_reramp PROPERTIES CUDA_SEPARABLE_COMPILATION ON)

# Add include directories for all targets
target_include_directories(readgeotiff PRIVATE "include")
target_include_directories(deramp_burst PRIVATE "include")
target_include_directories(geo2rdr_reramp PRIVATE "include")
target_include_directories(crossmul PRIVATE "include")
target_include_directories(phase_similarity PRIVATE "include")
target_include_directories(goldstein PRIVATE "include" ${CUDAToolkit_INCLUDE_DIRS})

# Link libraries with platform-specific handling
target_link_libraries(readgeotiff PRIVATE TIFF::TIFF)
target_link_libraries(deramp_burst PRIVATE TIFF::TIFF SQLite3::SQLite3)
target_link_libraries(geo2rdr_reramp PRIVATE TIFF::TIFF SQLite3::SQLite3)
target_link_libraries(goldstein PRIVATE CUDA::cufft)

# Install CUDA executables into the Python package: s1proc/bin/
install(TARGETS readgeotiff deramp_burst geo2rdr_reramp crossmul phase_similarity goldstein
        RUNTIME DESTINATION bin
)
#install(TARGETS readgeotiff deramp_burst geo2rdr_reramp crossmul phase_similarity goldstein
#        RUNTIME_DEPENDENCY_SET deps
#        RUNTIME DESTINATION bin
#)

#if(WIN32)
#    install(RUNTIME_DEPENDENCY_SET deps
#        DESTINATION bin
#        DIRECTORIES 
#            "$ENV{CONDA_PREFIX}/bin"
#            "$ENV{CONDA_PREFIX}/Library/bin"
#            "${CUDAToolkit_BIN_DIR}"
#        PRE_EXCLUDE_REGEXES 
#            ".*kernel32.*"
#            ".*wpaxholder.*"
#            ".*[Aa]zure.*"
#            ".*[Hh]vsi.*"
#            ".*[Pp]dm.*"
#            "^[Ee][Xx][Tt]-[Mm][Ss]-.*"
#            "^[Aa][Pp][Ii]-[Mm][Ss]-.*"
#            "^[Ee][Mm][Ss][Mm][Pp][Cc][Mm].*"  
#            "^[Ee][Cc][Mm][Aa][Pp][Ii].*"
#    )
#endif()
