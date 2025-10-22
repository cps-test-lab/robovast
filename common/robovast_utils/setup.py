#!/usr/bin/env python3

from setuptools import find_packages, setup

PACKAGE_NAME = "robovast_utils"

setup(
    name=PACKAGE_NAME,
    version="1.0.0",
    packages=find_packages(),
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/" + PACKAGE_NAME]),
        ("share/" + PACKAGE_NAME, ["package.xml"]),
    ],
    install_requires=[
        "setuptools",
        "kubernetes",
        "PyYAML",
    ],
    zip_safe=True,
    maintainer="Frederik Pasch",
    maintainer_email="fred-labs@mailbox.org",
    description="Common commandline tools for roboVAST",
    license="Apache-2.0",
    tests_require=["pytest"],
    entry_points={
        "console_scripts": [
            "generate_floorplans = robovast_utils.generate_floorplans:main",
            "generate_variants = robovast_utils.generate_variants:main",
        ],
    },
    python_requires=">=3.6",
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: Apache Software License",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.6",
        "Programming Language :: Python :: 3.7",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
    ],
)
