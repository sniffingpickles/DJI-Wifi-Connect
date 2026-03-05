from setuptools import setup, find_packages

setup(
    name="dji-pocket3-control",
    version="0.1.0",
    description="Open-source control suite for DJI Pocket 3 camera",
    long_description=open("README.md").read(),
    long_description_content_type="text/markdown",
    url="https://github.com/sniffingpickles/DJI-Wifi-Connect",
    license="MIT",
    packages=find_packages(),
    python_requires=">=3.11",
    install_requires=[
        "bleak>=0.21.0",
    ],
    entry_points={
        "console_scripts": [
            "pocket3=pocket3.main:main",
        ],
    },
    classifiers=[
        "Development Status :: 3 - Alpha",
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python :: 3",
        "Topic :: Multimedia :: Video",
    ],
)
