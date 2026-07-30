from pathlib import Path

from setuptools import setup


setup(
    name="agent-resume",
    version="0.1.0",
    description=(
        "A lightweight quota-aware supervisor for Codex and Claude CLI workflows"
    ),
    long_description=Path("README.md").read_text(encoding="utf-8"),
    long_description_content_type="text/markdown",
    license="MIT",
    author="Agent Resume contributors",
    packages=["agent_resume", "agent_resume.providers"],
    python_requires=">=3.9",
    install_requires=[],
    entry_points={"console_scripts": ["agent-resume=agent_resume.cli:main"]},
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Environment :: Console",
        "License :: OSI Approved :: MIT License",
        "Operating System :: MacOS",
        "Operating System :: POSIX :: Linux",
        "Programming Language :: Python :: 3",
    ],
)
