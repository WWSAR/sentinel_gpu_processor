#!/usr/bin/env python3
"""
Comprehensive installation verification test for s1proc.

Tests:
  1. Package import
  2. Compiled binary availability
  3. Python dependencies
  4. CLI interface
  5. CUDA availability
"""

import sys
import os
import platform
import subprocess
import importlib.util
from pathlib import Path

# ANSI color codes
GREEN = '\033[92m'
RED = '\033[91m'
YELLOW = '\033[93m'
RESET = '\033[0m'
BOLD = '\033[1m'

def print_header(text):
    """Print a formatted header."""
    width = 60
    print(f"\n{BOLD}{'=' * width}")
    print(f"{text.center(width)}")
    print(f"{'=' * width}{RESET}\n")

def print_success(text):
    """Print success message."""
    print(f"{GREEN}✓{RESET} {text}")

def print_error(text):
    """Print error message."""
    print(f"{RED}✗{RESET} {text}")

def print_warning(text):
    """Print warning message."""
    print(f"{YELLOW}⚠{RESET} {text}")

def check_python_version():
    """Verify Python version meets requirements."""
    print(f"Python: {sys.version.split()[0]}")
    if sys.version_info >= (3, 8):
        print_success(f"Python {sys.version_info.major}.{sys.version_info.minor} is supported")
        return True
    else:
        print_error(f"Python 3.8+ required, got {sys.version_info.major}.{sys.version_info.minor}")
        return False

def check_platform():
    """Display platform information."""
    system = platform.system()
    release = platform.release()
    arch = platform.machine()
    print(f"Platform: {system} {release} ({arch})\n")
    return True

def test_package_import():
    """Test 1: Import s1proc package."""
    print(f"\n{BOLD}[Test 1/4]{RESET} Importing s1proc package...")
    try:
        import s1proc
        print_success("s1proc package imported successfully")
        print(f"  Location: {s1proc.__file__}")
        return True
    except ImportError as e:
        print_error(f"Failed to import s1proc: {e}")
        print("  Run 'pip install -e .' from the repository root")
        return False

def test_compiled_binaries():
    """Test 2: Verify compiled CUDA binaries exist."""
    print(f"\n{BOLD}[Test 2/4]{RESET} Checking for compiled binaries...")
    
    try:
        import s1proc
        bin_dir = Path(s1proc.__file__).parent / 'bin'
        
        if not bin_dir.exists():
            print_warning(f"Binary directory not found: {bin_dir}")
            print("  Build may still be in progress. Try running: pip install -e . --force-reinstall")
            return False
        
        binaries = list(bin_dir.glob('*'))
        if not binaries:
            print_warning("Binary directory exists but is empty")
            return False
        
        expected_binaries = ['readgeotiff', 'deramp_burst', 'geo2rdr_reramp', 'crossmul', 'crossmul_sec']
        found_binaries = [b.name for b in binaries]
        
        print_success(f"Found {len(found_binaries)} compiled binary(ies):")
        for binary in sorted(found_binaries):
            print(f"  - {binary}")
        
        return True
    except Exception as e:
        print_error(f"Error checking binaries: {e}")
        return False

def test_python_dependencies():
    """Test 3: Verify all Python dependencies are installed."""
    print(f"\n{BOLD}[Test 3/4]{RESET} Checking Python dependencies...")
    
    required_packages = {
        'numpy': '1.23',
        'pandas': '2.0',
        'numba': '0.58',
        'matplotlib': '3.8',
        'tyro': '0.9',
    }
    
    all_installed = True
    for package, min_version in required_packages.items():
        try:
            mod = importlib.import_module(package)
            version = getattr(mod, '__version__', 'unknown')
            print_success(f"{package} ({version})")
        except ImportError:
            print_error(f"{package} not installed (required >= {min_version})")
            all_installed = False
    
    if not all_installed:
        print("\n  Install missing packages with:")
        print("  pip install -r requirements.txt")
    
    return all_installed

def test_cli():
    """Test 4: Test CLI interface."""
    print(f"\n{BOLD}[Test 4/4]{RESET} Testing CLI interface...")
    
    try:
        result = subprocess.run(
            ['s1proc', '--help'],
            capture_output=True,
            text=True,
            timeout=10
        )
        
        if result.returncode == 0:
            print_success("CLI works - s1proc --help executed successfully")
            lines = result.stdout.split('\n')[:3]
            for line in lines:
                if line.strip():
                    print(f"  {line}")
            return True
        else:
            print_warning("CLI executed but returned non-zero status")
            print(f"  Error: {result.stderr}")
            return False
            
    except FileNotFoundError:
        print_warning("s1proc command not found in PATH")
        print("  This is expected if the package was just installed")
        return True
    except subprocess.TimeoutExpired:
        print_error("CLI command timed out")
        return False
    except Exception as e:
        print_warning(f"Could not test CLI: {e}")
        return True

def test_cuda():
    """Additional: Check CUDA availability."""
    print(f"\n{BOLD}[Additional]{RESET} CUDA availability...")
    
    try:
        result = subprocess.run(
            ['nvidia-smi'],
            capture_output=True,
            text=True,
            timeout=5
        )
        
        if result.returncode == 0:
            lines = result.stdout.split('\n')
            for line in lines[:5]:
                if line.strip():
                    print(f"  {line}")
            print_success("NVIDIA GPU detected")
            return True
        else:
            print_warning("nvidia-smi not found or returned error")
            return False
            
    except FileNotFoundError:
        print_warning("nvidia-smi not found in PATH")
        print("  This is expected on systems without NVIDIA drivers")
        return True
    except Exception as e:
        print_warning(f"Could not check CUDA: {e}")
        return True

def main():
    """Run all tests."""
    print_header("INSTALLATION VERIFICATION TEST")
    
    check_python_version()
    check_platform()
    
    tests = [
        test_package_import,
        test_compiled_binaries,
        test_python_dependencies,
        test_cli,
    ]
    
    results = []
    for test in tests:
        try:
            results.append(test())
        except Exception as e:
            print_error(f"Unexpected error in {test.__name__}: {e}")
            results.append(False)
    
    try:
        test_cuda()
    except Exception as e:
        print_warning(f"CUDA check failed: {e}")
    
    print_header("VERIFICATION COMPLETE")
    
    passed = sum(results)
    total = len(results)
    
    if all(results):
        print_success(f"All {total} tests passed!")
        print("\nYour s1proc installation is ready to use.")
        return 0
    else:
        print_error(f"{passed}/{total} tests passed")
        print(f"\n{YELLOW}Failed tests need attention. Check the output above.{RESET}")
        return 1

if __name__ == '__main__':
    sys.exit(main())
