#!/usr/bin/env bash
# kwin-mcp 启动器：补上 NixOS 上缺的两样东西，再 exec 真正的 MCP server。
#
# 为什么不直接用 nix-shell 启动：本机全局 nix 配置会往 **stdout** 打
# shellHook 横幅（"blue devShell → …"），而 stdout 是 MCP 的 JSON-RPC 通道，
# 多一行非 JSON 就会破坏协议。所以这里只借用 nix 推导出的路径，自己拼环境。
#
# 需要的东西：
#   - GI_TYPELIB_PATH：PyGObject 加载 GObject/Gio/Atspi 的类型库
#   - LD_LIBRARY_PATH：kwin-mcp 用 ctypes 按名字 dlopen("libei.so.1")
#   - PATH：截图回退用的 spectacle
set -euo pipefail

GI_TYPELIB_PATH="/nix/store/pgc3a4xwls7lklxmhvdgrb0zh5yjnhxs-glib-2.88.3/lib/girepository-1.0"
GI_TYPELIB_PATH="$GI_TYPELIB_PATH:/nix/store/s0z5k5fbrjwfd32q67z09v4iq4cssxng-gobject-introspection-wrapped-1.86.0/lib/girepository-1.0"
GI_TYPELIB_PATH="$GI_TYPELIB_PATH:/nix/store/95nvlclky1pk58n42xpk9jdxmvzcy5ys-at-spi2-core-2.60.7/lib/girepository-1.0"
GI_TYPELIB_PATH="$GI_TYPELIB_PATH:/nix/store/ax5sp90axn57clpkx5jr1sbnfr44m5y9-harfbuzz-13.2.1/lib/girepository-1.0"
GI_TYPELIB_PATH="$GI_TYPELIB_PATH:/nix/store/52ymrn87iihhr9sy9c6yzyxbx87s0dzb-libproxy-0.5.12/lib/girepository-1.0"
GI_TYPELIB_PATH="$GI_TYPELIB_PATH:/nix/store/a1kncrkc0j17h8qfnayxhhax0jdqm8vz-gobject-introspection-1.86.0/lib/girepository-1.0"
export GI_TYPELIB_PATH

# libei 走 nix 现算（GC 后路径会变），失败时退回已知路径。
libei="$(nix eval --raw nixpkgs#libei.outPath 2>/dev/null || true)"
if [ -z "${libei}" ] || [ ! -d "${libei}/lib" ]; then
  libei="/nix/store/gafbmy1hrwkn7b4xjysxr0120mdx55h2-libei-1.5.0"
fi
export LD_LIBRARY_PATH="${libei}/lib${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"

# 截图回退路径（KWin ScreenShot2 需要 portal 授权，拿不到时用 spectacle）。
spectacle_bin="/nix/store/2bccf4k1sqm290sy4sdwapmywhp2v89l-spectacle-6.6.6/bin"
if [ -d "${spectacle_bin}" ]; then
  export PATH="${spectacle_bin}:${PATH}"
fi

bin="${HOME}/.local/share/uv/tools/kwin-mcp/bin/kwin-mcp"
if [ ! -x "${bin}" ]; then
  echo "kwin-mcp 未安装：先运行 uv tool install kwin-mcp" >&2
  exit 1
fi
exec "${bin}" "$@"
