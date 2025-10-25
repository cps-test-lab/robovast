#!/usr/bin/env python3

from setuptools import find_packages, setup

PACKAGE_NAME = 'robovast_common'

setup(
    name=PACKAGE_NAME,
    version='1.0.0',
    packages=find_packages(),
    data_files=[
        ('share/ament_index/resource_index/packages',
            ['resource/' + PACKAGE_NAME]),
        ('share/' + PACKAGE_NAME, ['package.xml']),
    ],
    install_requires=[
        'setuptools',
    ],
    zip_safe=True,
    maintainer='Frederik Pasch',
    maintainer_email='fred-labs@mailbox.org',
    description='Common components for RoboVast applications',
    license='Apache-2.0',
    tests_require=['pytest'],
    entry_points={
        'console_scripts': [
        ],
        "robovast.variation_types": [
            "ParameterVariationDistributionUniform = robovast_common.variation.parameter_variation:ParameterVariationDistributionUniform",
            "ParameterVariationDistributionGaussian = robovast_common.variation.parameter_variation:ParameterVariationDistributionGaussian",
            "ParameterVariationList = robovast_common.variation.parameter_variation:ParameterVariationList",
        ]
    },
    python_requires=">=3.6",
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: Apache Software License",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
    ],
)
