#!/usr/bin/env python
# -*- encoding: utf-8 -*-
"""
Setup script matching yaronzz/Tidal-Media-Downloader structure.
"""
from setuptools import setup, find_packages

VERSION = "2026.1.0"

setup(
    name="tidal-dl",
    version=VERSION,
    license="Apache2",
    description="TIDAL Music Downloader & Resolution Engine.",
    author="YaronH",
    packages=find_packages(),
    include_package_data=True,
    platforms="any",
    install_requires=[
        "httpx>=0.23.3",
        "requests>=2.22.0",
        "mutagen>=1.45.0",
        "pycryptodome>=3.19.0",
        "pydantic>=2.0.0",
        "xmltodict>=0.13.0",
    ],
    entry_points={
        "console_scripts": [
            "tidal-dl = tidal.cli:main",
        ]
    },
)
