from setuptools import setup, find_packages

setup(
    name="ComfyUI-PreviewBridgeExtended",
    version="0.1.0",
    description="Enhanced Preview Bridge node with optional mask input - Part of DazzleNodes",
    author="Dustin",
    author_email="6962246+djdarcy@users.noreply.github.com",
    url="https://github.com/DazzleNodes/ComfyUI-PreviewBridgeExtended",
    packages=find_packages(),
    install_requires=[
        "torch",
    ],
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
    ],
    python_requires=">=3.10",
)
