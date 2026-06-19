@echo off
set

echo "=== Check Local Compilers ==="
echo %BUILD_PREFIX%
where cl
where nvcc

:: ==========================================
:: 1. Compile cpp and cuda code using cmake
:: ==========================================
cmake -S csrc -B build -G Ninja ^
    -DCMAKE_BUILD_TYPE=Release ^
    -DCMAKE_INSTALL_PREFIX="%SRC_DIR%\s1proc" ^
    -DCMAKE_PREFIX_PATH="%PREFIX%"
if errorlevel 1 exit 1

cmake --build build --target install
if errorlevel 1 exit 1

:: ==========================================
:: 2. Copy external dependencies to the output directory
:: ==========================================
echo "=== Copying External Dependencies ==="
copy "%SRC_DIR%\extern\*" "%SRC_DIR%\s1proc\bin\" /Y
if errorlevel 1 exit 1

:: ==========================================
:: 3. Install the package using pip
:: ==========================================
"%PYTHON%" -m pip install . --no-deps --ignore-installed --no-cache-dir -vv
if errorlevel 1 exit 1