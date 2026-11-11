#!/bin/sh
# 编译一次后正式运行复用本机二进制，不在每次连接时启动Swift编译器。
set -eu
project_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
mkdir -p "$project_root/.local/tools"
swiftc "$project_root/src/job_hunter/browser/chrome_permission.swift" -o "$project_root/.local/tools/chrome-permission"
"$project_root/.local/tools/chrome-permission" --self-test
