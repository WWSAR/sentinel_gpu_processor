cmake_minimum_required(VERSION 3.21)

project(snaphu C)

# Set C standard
set(CMAKE_C_STANDARD 99)

# Optimization flags
if(NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE Release)
endif()

# Source files
set(SOURCES
    src/snaphu.c
    src/snaphu_tile.c
    src/snaphu_solver.c
    src/snaphu_io.c
    src/snaphu_util.c
    src/snaphu_cost.c
    src/snaphu_cs2.c
    src/snaphu.h
    src/snaphu_cs2types.h
)


# Create executable
add_executable(snaphu ${SOURCES})

# Compiler warnings
target_compile_options(snaphu PRIVATE -O3 -Wall)

# Link math library on Unix-like systems
target_link_libraries(snaphu m)

# Output directory (equivalent to ../bin)
set_target_properties(snaphu PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}/bin
)

# Install rules
install(TARGETS snaphu
    RUNTIME DESTINATION bin
)

