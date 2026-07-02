# sentinel_gpu_processor

Sentinel-1 Processor rewritten using Python and CUDA.

> **📖 [User Guide](docs/user_guide.md)** — full walkthrough of the processing
> pipeline, from initialisation to deformation time series.

## Installation

The package is under active development. We plan to publish it on conda-forge in the future. For now, install from source:

### Prerequisites

- NVIDIA GPU (CUDA version 12.4+ recommended)
- [conda](https://docs.conda.io/en/latest/miniconda.html) or [mamba](https://mamba.readthedocs.io/)
- **Windows users** must install **MSVC** (Microsoft Visual C++). Compilation must be done in **x64 Native Tools Command Prompt for VS Insiders** or **x64_x86 Cross Tools Command Prompt for VS Insiders**. If your Visual Studio is too new (e.g., VS 2026), you need to install the older **MSVC v143 - VS 2022** toolchain and activate it by running:

  ```
  "C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvarsall.bat" x64 -vcvars_ver=14.44.35207
  ```

  (Adjust the path to match your Visual Studio installation.)

### Steps

**1. Clone the repository**

```bash
git clone https://github.com/WWSAR/sentinel_gpu_processor.git
cd sentinel_gpu_processor
```

**2. Create and activate a conda environment**

```bash
conda create -n s1test python=3.11 -y
conda activate s1test
```

**3. Install mamba (strongly recommended)**

```bash
conda install -c conda-forge mamba -y
```

*mamba is strongly recommended because conda can fail to solve the environment on older systems (e.g., CentOS 7).*

**4. Install Python dependencies**

```bash
mamba install --file requirements.txt -y
```

**Alternative for steps 2–4 (modern systems with CUDA >= 12.4):**

If you are on a relatively new Windows or Ubuntu system and your CUDA version is 12.4 or newer (check with `nvidia-smi`), you can replace steps 2–4 with a single command:

```bash
conda env create -f environment.yml
```

**5. Compile CUDA executables**

```bash
cmake -S csrc -B csrc/build -G Ninja -DCMAKE_INSTALL_PREFIX=s1proc -DCMAKE_BUILD_TYPE=Release
cmake --build csrc/build --target install
```

**6. Install the package in editable mode**

```bash
pip install -e .
```
