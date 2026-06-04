cmake_minimum_required(VERSION 3.21)
project(s1proc VERSION 0.1.0 LANGUAGES C CXX CUDA)
set(CMAKE_CXX_STANDARD 11)
set(CMAKE_CXX_STANDARD_REQUIRED True)

# Automatically set architectures based on specific flags
set(CMAKE_CUDA_ARCHITECTURES 60 70 75 80 89 90)

#if(DEFINED ENV{CONDA_PREFIX})
#    include_directories("$ENV{CONDA_PREFIX}/include")
#    link_directories("$ENV{CONDA_PREFIX}/lib")
#endif()

# Find packages (order matters: tries custom paths first, then system)
find_package(TIFF REQUIRED)
find_package(SQLite3 REQUIRED)
find_package(CUDAToolkit REQUIRED)
#find_package(CUDAToolkit REQUIRED COMPONENTS cufft)

#message(STATUS "PREFIX=$ENV{PREFIX}")
#message(STATUS "CMAKE_PREFIX_PATH=${CMAKE_PREFIX_PATH}")
#find_path(
#    CUFFT_INCLUDE_DIR
#    cufft.h
#)

message(STATUS "CUDAToolkit_INCLUDE_DIRS=${CUDAToolkit_INCLUDE_DIRS}")
message(STATUS "CUDAToolkit_BIN_DIR=${CUDAToolkit_BIN_DIR}")
# Add source files

add_executable(readgeotiff src/readgeotiff.cpp)
add_executable(deramp_burst src/deramp_burst.cu src/sql_mod.cpp
               src/orbit.cu src/sario.cu)
add_executable(geo2rdr_reramp src/geo2rdr_reramp.cu src/sql_mod.cpp
               src/orbit.cu src/sario.cu src/bounds.cu)
add_executable(crossmul src/crossmul.cu src/sario.cu)
add_executable(crossmul_sec src/crossmul_sec.cu src/sario.cu)
add_executable(multilook_amp src/multilook_amp.cu src/sario.cu)
add_executable(phase_similarity src/phase_similarity.cu)
add_executable(goldstein src/goldstein.cu)

set_target_properties(geo2rdr_reramp PROPERTIES CUDA_SEPARABLE_COMPILATION ON)

# Add include directories for all targets
target_include_directories(readgeotiff PRIVATE "include")
target_include_directories(deramp_burst PRIVATE "include")
target_include_directories(geo2rdr_reramp PRIVATE "include")
target_include_directories(crossmul PRIVATE "include")
target_include_directories(crossmul_sec PRIVATE "include")
target_include_directories(multilook_amp PRIVATE "include")
target_include_directories(phase_similarity PRIVATE "include")
target_include_directories(goldstein PRIVATE "include")

# Link libraries with platform-specific handling
target_link_libraries(readgeotiff PRIVATE TIFF::TIFF)
target_link_libraries(deramp_burst PRIVATE TIFF::TIFF SQLite3::SQLite3)
target_link_libraries(geo2rdr_reramp PRIVATE TIFF::TIFF SQLite3::SQLite3)
target_link_libraries(goldstein PRIVATE CUDA::cufft)

# Install CUDA executables into the Python package: s1proc/bin/
install(TARGETS readgeotiff deramp_burst geo2rdr_reramp crossmul crossmul_sec multilook_amp phase_similarity goldstein
        RUNTIME_DEPENDENCY_SET deps
        RUNTIME DESTINATION bin
)

if(WIN32)
    install(RUNTIME_DEPENDENCY_SET deps
        DESTINATION bin
        DIRECTORIES 
            "$ENV{CONDA_PREFIX}/bin"
            "$ENV{CONDA_PREFIX}/Library/bin"
            "${CUDAToolkit_BIN_DIR}"
        PRE_EXCLUDE_REGEXES 
            ".*kernel32.*"
            ".*wpaxholder.*"
            ".*[Aa]zure.*"
            ".*[Hh]vsi.*"
            ".*[Pp]dm.*"
            "^[Ee][Xx][Tt]-[Mm][Ss]-.*"
            "^[Aa][Pp][Ii]-[Mm][Ss]-.*"
            "^[Ee][Mm][Ss][Mm][Pp][Cc][Mm].*"  
            "^[Ee][Cc][Mm][Aa][Pp][Ii].*"
    )
endif()