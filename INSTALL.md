# Installation Guide

## System Requirements

### Minimum Requirements
- **Python**: 3.8 or later
- **CMake**: 3.5 or later
- **CUDA Toolkit**: 11.0 or later (for GPU support)
- **Compiler**: GCC 7+ (Linux), MSVC 2019+ (Windows), Clang 10+ (macOS)

### Disk Space
- ~2 GB for CUDA Toolkit
- ~500 MB for development dependencies
- ~200 MB for the project and build artifacts

---

## Quick Start

### Linux (Ubuntu/Debian)
```bash
# Install system dependencies
sudo apt-get update
sudo apt-get install -y build-essential cmake cuda-toolkit libtiff-dev sqlite3 libsqlite3-dev

# Clone and install
git clone https://github.com/WWSAR/sentinel_gpu_processor.git
cd sentinel_gpu_processor
pip install -r requirements.txt
pip install -e .

# Verify installation
python test_install.py
```

### Windows
```powershell
# 1. Install Visual Studio Build Tools, CMake, and CUDA Toolkit (see detailed instructions below)

# 2. Setup vcpkg for dependencies
git clone https://github.com/Microsoft/vcpkg.git
.\vcpkg\bootstrap-vcpkg.bat
.\vcpkg\vcpkg install tiff:x64-windows sqlite3:x64-windows
set VCPKG_ROOT=%cd%\vcpkg

# 3. Install Python dependencies and package
git clone https://github.com/WWSAR/sentinel_gpu_processor.git
cd sentinel_gpu_processor
pip install -r requirements.txt
pip install -e .

# 4. Verify installation
python test_install.py
```

### macOS
```bash
# Install with Homebrew
brew install cmake cuda tiff sqlite3

# Clone and install
git clone https://github.com/WWSAR/sentinel_gpu_processor.git
cd sentinel_gpu_processor
pip install -r requirements.txt
pip install -e .

# Verify installation
python test_install.py
```

---

## Detailed Platform Setup

### Ubuntu/Debian Linux

#### 1. Install Build Tools
```bash
sudo apt-get update
sudo apt-get install -y \
  build-essential \
  cmake \
  git \
  python3-dev \
  python3-pip
```

#### 2. Install CUDA Toolkit
Visit [NVIDIA CUDA Downloads](https://developer.nvidia.com/cuda-downloads) or:
```bash
# For Ubuntu 22.04
wget https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2204/x86_64/cuda-keyring_1.0-1_all.deb
sudo dpkg -i cuda-keyring_1.0-1_all.deb
sudo apt-get update
sudo apt-get install -y cuda-toolkit-11-8

# Add CUDA to PATH
echo 'export PATH=/usr/local/cuda-11.8/bin:$PATH' >> ~/.bashrc
echo 'export LD_LIBRARY_PATH=/usr/local/cuda-11.8/lib64:$LD_LIBRARY_PATH' >> ~/.bashrc
source ~/.bashrc
```

#### 3. Install Required Libraries
```bash
sudo apt-get install -y \
  libtiff-dev \
  sqlite3 \
  libsqlite3-dev
```

#### 4. Install s1proc
```bash
git clone https://github.com/WWSAR/sentinel_gpu_processor.git
cd sentinel_gpu_processor
pip install -r requirements.txt
pip install -e .
```

---

### Windows 10/11

#### 1. Install Visual Studio Build Tools
- Download [Visual Studio Build Tools](https://visualstudio.microsoft.com/visual-cpp-build-tools/)
- Install with **C++ build tools** workload
- Ensure **Windows 10 SDK** is selected

#### 2. Install CMake
- Download from [cmake.org](https://cmake.org/download/)
- Choose "Visual Studio 16 2019" or later during installation
- Add to PATH: `C:\Program Files\CMake\bin`

#### 3. Install CUDA Toolkit
- Download from [NVIDIA CUDA Downloads](https://developer.nvidia.com/cuda-downloads)
- Select Windows → x86_64 → your Windows version
- Run installer (default installation is fine)
- Verify:
  ```powershell
  nvcc --version
  ```

#### 4. Setup vcpkg (Dependency Manager)
```powershell
# Clone vcpkg
cd C:\
git clone https://github.com/Microsoft/vcpkg.git
cd vcpkg

# Bootstrap
.\bootstrap-vcpkg.bat

# Install dependencies
.\vcpkg install tiff:x64-windows sqlite3:x64-windows

# Set environment variable (permanent)
[Environment]::SetEnvironmentVariable("VCPKG_ROOT", "C:\vcpkg", "User")
$env:VCPKG_ROOT = "C:\vcpkg"
```

#### 5. Install s1proc
```powershell
git clone https://github.com/WWSAR/sentinel_gpu_processor.git
cd sentinel_gpu_processor
pip install -r requirements.txt
pip install -e .
```

#### 6. Verify
```powershell
python test_install.py
```

---

### macOS

#### 1. Install Homebrew (if not already installed)
```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

#### 2. Install Dependencies
```bash
brew install cmake
brew install cuda
brew install libtiff
brew install sqlite
```

#### 3. Set Environment Variables
```bash
# Add to ~/.zprofile (macOS 11+) or ~/.bash_profile
export CUDA_PATH=/usr/local/cuda
export PATH=$CUDA_PATH/bin:$PATH
export DYLD_LIBRARY_PATH=$CUDA_PATH/lib:$DYLD_LIBRARY_PATH
```

#### 4. Install s1proc
```bash
git clone https://github.com/WWSAR/sentinel_gpu_processor.git
cd sentinel_gpu_processor
pip install -r requirements.txt
pip install -e .
```

---

## Troubleshooting

### "cmake: command not found"
**Solution**: CMake is not in PATH
```bash
# Linux
sudo apt-get install cmake

# macOS
brew install cmake

# Windows: Reinstall CMake and select "Add to PATH" during setup
```

### "nvcc: command not found"
**Solution**: CUDA Toolkit not installed or not in PATH
- Install CUDA Toolkit from [nvidia.com](https://developer.nvidia.com/cuda-downloads)
- Add CUDA to PATH:
  ```bash
  export PATH=/usr/local/cuda/bin:$PATH
  ```

### "error: 'vcpkg.cmake' not found" (Windows)
**Solution**: vcpkg not properly set up
```powershell
[Environment]::SetEnvironmentVariable("VCPKG_ROOT", "C:\path\to\vcpkg", "User")
```

### "pip install -e . fails during build"
**Solution**: Missing build dependencies
```bash
# Linux
sudo apt-get install python3-dev

# Retry
pip install -e .
```

---

## Support

- **GitHub Issues**: [Report bugs](https://github.com/WWSAR/sentinel_gpu_processor/issues)
- **NVIDIA CUDA Support**: [CUDA Documentation](https://docs.nvidia.com/cuda/)
