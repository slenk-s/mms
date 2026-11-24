"""Cython build script for pyd package"""
import os
import sys
from distutils.core import Extension, setup
from Cython.Build import cythonize

PYD_DIR = os.path.dirname(os.path.abspath(__file__))
CLIENT_DIR = os.path.dirname(PYD_DIR)

sys.path.insert(0, CLIENT_DIR)

setup(
    name="pyd",
    packages=["pyd"],
    package_dir={"pyd": PYD_DIR},
    ext_modules=cythonize(
        [
            Extension(
                "pyd." + module_name,
                [os.path.relpath(
                    os.path.join(PYD_DIR, module_name + ".pyx"),
                    os.getcwd(),
                )],
            )
            for module_name in ("app_config", "credential_manager", "mysql_client")
        ],
        compiler_directives={"language_level": "3"},
        build_dir=os.path.join(PYD_DIR, "_build_c"),
    ),
    script_args=["build_ext", "--inplace"],
)