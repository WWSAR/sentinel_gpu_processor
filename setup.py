import os
import subprocess
import sys
import shutil
from pathlib import Path
from setuptools import setup, Extension
from setuptools.command.build_ext import build_ext

class CMakeBuild(build_ext):
    def run(self):
        build_dir = Path(self.build_temp)
        build_dir.mkdir(parents=True, exist_ok=True)
        ext_dir = Path(self.get_ext_fullpath(self.extensions[0].name)).parent.resolve()

        # Configure and build with CMake
        if os.name == "nt":
            subprocess.check_call(
                ["cmake", "-S", ".", "-B", str(build_dir), f"--preset default",
                 f"-DCMAKE_LIBRARY_OUTPUT_DIRECTORY={ext_dir}",
                 f"-DCMAKE_INSTALL_PREFIX={ext_dir}"]
            )
            subprocess.check_call(["cmake", "--build", str(build_dir), "--config", "Release"])
            subprocess.check_call(["cmake", "--install", str(build_dir), "--config", "Release"])
        else:
            subprocess.check_call(
                ["cmake", "-S", ".", "-B", str(build_dir), f"-DCMAKE_LIBRARY_OUTPUT_DIRECTORY={ext_dir}",
                 f"-DCMAKE_INSTALL_PREFIX={ext_dir}"]
            )
            subprocess.check_call(["cmake", "--build", str(build_dir)])
            subprocess.check_call(["cmake", "--install", str(build_dir)])
        super().run()

# CMake will have built the .so/.pyd directly into ext_dir — no need to copy manually

if os.name == 'nt':
    setup(
        name="s1proc",
        version="0.1.0",
        packages=["s1proc"],
        cmdclass={"build_ext": CMakeBuild},
        include_package_data=True,
        package_data={
            "s1proc": ["bin/*"],
        }
    )
else:
    setup(
        name="s1proc",
        version="0.1.0",
        packages=["s1proc"],
        ext_modules=[Extension("s1proc_cuda", sources=[])],
        cmdclass={"build_ext": CMakeBuild},
        include_package_data=True,
        package_data={
            "s1proc": ["bin/*"],
        }
    )

