{
  description = "blue —— Infinity 系统开发环境（uv + RabbitMQ）";

  inputs = {
    # 固定到 NixOS 官方发布 tarball：既不用 GitHub，也不依赖本机 registry。
    # `nixpkgs.url = "nixpkgs"` 是 indirect 写法，结果取决于每台机器的 registry，
    # 公开仓库里必须钉死；flake.lock 里同时记了 narHash，下载内容不会漂移。
    # 升级：换成新的 release tarball URL，再 `nix flake lock`。
    nixpkgs.url = "https://releases.nixos.org/nixpkgs/nixpkgs-26.11pre1107035.f9887a8a434b/nixexprs.tar.zst";
  };

  outputs =
    { nixpkgs, ... }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "x86_64-darwin"
        "aarch64-darwin"
      ];
    in
    {
      devShells = nixpkgs.lib.genAttrs systems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
        in
        {
          default = pkgs.mkShell {
            name = "blue";

            packages = with pkgs; [
              uv # Python 依赖 / 虚拟环境管理
              python314 # 与 .python-version 中的 3.14 对齐
              rabbitmq-server # RabbitMQ 4.x（自带 Erlang 运行时）
              git
              curl
              jq
            ];

            shellHook = ''
              export PROJECT_ROOT="''${PROJECT_ROOT:-$PWD}"
              export RABBITMQ_DATA_DIR="$PROJECT_ROOT/.rabbitmq"

              # 数据、日志、插件统一放在项目内，不污染用户目录
              export RABBITMQ_MNESIA_BASE="$RABBITMQ_DATA_DIR/mnesia"
              export RABBITMQ_LOG_BASE="$RABBITMQ_DATA_DIR/log"
              export RABBITMQ_PID_FILE="$RABBITMQ_DATA_DIR/pid"
              export RABBITMQ_ENABLED_PLUGINS_FILE="$RABBITMQ_DATA_DIR/enabled_plugins"

              # 只监听本机回环地址（AMQP 5672 / 管理台 15672）
              export RABBITMQ_CONFIG_FILE="$RABBITMQ_DATA_DIR/rabbitmq"

              # 应用侧默认连接串
              export RABBITMQ_URL="amqp://guest:guest@127.0.0.1:5672/"

              mkdir -p "$RABBITMQ_MNESIA_BASE" "$RABBITMQ_LOG_BASE"
              if [ ! -f "$RABBITMQ_CONFIG_FILE.conf" ]; then
                cat > "$RABBITMQ_CONFIG_FILE.conf" <<'RABBITMQ_CONF'
listeners.tcp.1 = 127.0.0.1:5672
management.tcp.ip = 127.0.0.1
RABBITMQ_CONF
              fi
              if [ ! -f "$RABBITMQ_ENABLED_PLUGINS_FILE" ]; then
                echo '[rabbitmq_management].' > "$RABBITMQ_ENABLED_PLUGINS_FILE"
              fi

              echo "blue devShell → uv $(uv --version | cut -d' ' -f2) | $(python3.14 --version) | RabbitMQ ${pkgs.rabbitmq-server.version}"
              echo "  uv sync            # 安装/同步 Python 依赖"
              echo "  rabbitmq-server    # 前台启动 RabbitMQ（管理台 http://127.0.0.1:15672，guest/guest）"
              echo "  rabbitmqctl status # 查看节点状态"
            '';
          };
        }
      );
    };
}
